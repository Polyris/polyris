"""AssetFanout: wake the tasks that wait on a new asset fact (ADR #130 §6.1, ADR #137 §7).

An event source mapping feeds it the asset-events stream (INSERT records
only). For each new fact it makes one narrow pass of the dispatcher over each
run with a task subscribed to `asset:{name}`: the rows its pointers name in
that run and their direct upstreams. The pass reads recent facts from the base
table with a consistent read, so the decision does not depend on how quickly
the partition index catches up.

It also starts the pipelines scheduled on the asset (push): each consumer's
whole `schedule=[...]` is evaluated for the fact's own partition with the
same rule as pull, `.within()` as of the fact's time, and a satisfied
consumer is started under a deterministic name — one run per date for an
AND schedule, one per event for OR or `cascade='all'` — so a repeated
record never starts a second run.

Pull wakes are hints: a pass that fails transiently is logged and left to its
run row's next check. A failure for one consumer never fails the record: a transient
start failure becomes a push retry row for the sweeper, anything else a
warning. A record is reported back for redelivery only when its subscribers
could not be read (transiently), when even a retry row could not be
written, or when the time budget ran out before it was reached; redelivery
is safe because every action is idempotent.
"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

import env
from polyris.coordination import asset_spec as sp
from polyris.coordination.asset_conditions import evaluate as evaluate_assets
from polyris.coordination.records import FACT_VERSION, fact_from_item, push_retry_item, warning_item
from polyris.coordination.run_name import KIND_AND, KIND_EVENT, run_name
from polyris.coordination.times import parse_utc
from failures import CoordinationTransient, error_code, is_condition_failed, is_transient, reraise_transient
from logger import log
from dispatcher import Dispatcher
from polyris.constants import WarningKind
from polyris.coordination import run_row, start_run
from stores import FactStore, Registry

#: Stop starting new records with this much of the invocation left.
RESERVE_MS = 10_000
#: Inside a record, stop with this much left: the rest are lost hints, which the sweeper's checks and
#: the hourly push catch-up recover, so one record never times the function out and blocks its shard.
RECORD_RESERVE_MS = 5_000
_deserializer = TypeDeserializer()


def fact_of(record: Dict[str, Any]) -> Dict[str, Any]:
    """The asset-events item a stream record carries."""
    image = record["dynamodb"]["NewImage"]
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


class AssetFanout:
    """The fan-out of one fact, with the dispatcher whose tables, clients and clock it shares."""

    def __init__(self, dispatcher: Dispatcher, *, subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
                 consumers_table: str = env.ASSET_SUBSCRIPTIONS_TABLE):
        self.dispatcher = dispatcher
        resource = dispatcher.resource
        self.tasks = resource.Table(dispatcher.tokens_table)
        self.subscriptions = resource.Table(subscriptions_table)
        self.consumers = resource.Table(consumers_table)
        self.registry_table = resource.Table(dispatcher.pipelines_table)

    def handle(self, records: List[Dict[str, Any]], remaining_ms: Callable[[], int]) -> Dict[str, Any]:
        """Process records in stream order; return the ones to redeliver.

        The stream redelivers from the first reported record, so processing
        stops there: later records are not handled twice.
        """
        for index, record in enumerate(records):
            if remaining_ms() < RESERVE_MS:
                log.warn("asset_fanout", "time budget reached; records left for redelivery", left=len(records) - index)
                return self._redeliver(records[index:])
            if record.get("eventName") != "INSERT":
                continue      # the mapping passes INSERTs only; anything else is not a new fact
            sequence = str(record["dynamodb"]["SequenceNumber"])
            try:
                fact = fact_of(record)
                if fact.get("fact_version") != FACT_VERSION:
                    # A writer before ADR #130 wrote it, and started its own consumers then.
                    log.info("asset_fanout", "a fact of a version before ADR #130; skipped", sequence=sequence,
                             asset_name=str(fact.get("asset_name", "")))
                    continue
                fact_from_item(fact, declared_granularity="daily")     # a fact nobody could read is poison
                # Push first: consumers are few and only the hourly catch-up replays a missed one, while
                # waiters are many and the sweeper re-checks every one the budget leaves (ADR #130 §6.1).
                self.push(fact, remaining_ms=remaining_ms)
                self.fan_out(fact, remaining_ms=remaining_ms)
            except CoordinationTransient as exc:
                log.error("asset_fanout", "a transient failure; the record is redelivered", sequence=sequence,
                          error=str(exc))
                return self._redeliver(records[index:])
            except Exception as exc:  # noqa: BLE001 — a poison record must not block its shard (ADR #130 §6.1)
                if isinstance(exc, (ClientError, BotoCoreError)) and is_transient(exc):
                    log.error("asset_fanout", "a transient failure; the record is redelivered", sequence=sequence,
                              error=str(exc))
                    return self._redeliver(records[index:])
                self._warn(f"record:{sequence}",
                           f"asset-events record {sequence} could not be processed: {type(exc).__name__}: {exc}")
        return {"batchItemFailures": []}

    @staticmethod
    def _redeliver(records: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"batchItemFailures": [{"itemIdentifier": str(r["dynamodb"]["SequenceNumber"])} for r in records]}

    def fan_out(self, fact: Dict[str, Any], *, remaining_ms: Optional[Callable[[], int]] = None) -> Dict[str, int]:
        """A narrow pass over each run with a task waiting on the fact's asset. Raises only CoordinationTransient."""
        asset = str(fact["asset_name"])
        pointers = self._subscribers(asset)
        runs: Dict[str, List[str]] = {}
        for subscriber, run_key in pointers:
            run_key = run_key or self._run_of(asset, subscriber)
            if run_key:
                runs.setdefault(run_key, []).append(subscriber)
        passed = lost = 0
        budget: Callable[[], int] = remaining_ms or (lambda: 10 ** 9)
        for index, (run_key, keys) in enumerate(runs.items()):
            if budget() < RECORD_RESERVE_MS:
                lost += len(runs) - index
                # Principle #41: a lost hint delays the task; its run row's next check decides it from facts.
                log.error("asset_fanout", "time budget reached inside a record; the runs' next checks take the rest",
                          asset_name=asset, left=len(runs) - index)
                break
            try:
                self.dispatcher.narrow_pass(run_key, keys, source="asset_fanout", remaining_ms=budget)
                passed += 1
            except CoordinationTransient as exc:
                lost += 1
                log.error("asset_fanout", "a pass failed transiently; the run's next check decides its rows",
                          run=run_key, asset_name=asset, error=str(exc))
        counts = {"subscribers": len(pointers), "runs": len(runs), "passed": passed, "lost": lost}
        log.info("asset_fanout", "fanned out", asset_name=asset,
                 partition_key=str(fact.get("partition_key", fact.get("execution_date", ""))), **counts)
        return counts

    def _run_of(self, asset: str, subscriber: str) -> Optional[str]:
        """The run of a pointer that names none, which 1.2 wrote: its row's run. A pointer whose row is gone is
        deleted (Rule 40); a row without a run row is then stopped by the pass (ADR #137 invariant 11)."""
        try:
            row = self.tasks.get_item(Key={run_row.TABLE_KEY: subscriber}, ConsistentRead=True).get("Item")
            if row is None:
                self.subscriptions.delete_item(Key={start_run.POINTER_KEY: f"asset:{asset}",
                                                    start_run.POINTER_SUBSCRIBER: subscriber})
                return None
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            log.error("asset_fanout", "the row of a pointer could not be read", subscriber=subscriber, error=str(exc))
            return None
        return run_row.key(str(row["pipeline_execution"])) if row.get("pipeline_execution") else None

    def push(self, fact: Dict[str, Any], *, remaining_ms: Optional[Callable[[], int]] = None) -> Dict[str, int]:
        """Start every push consumer the fact completes. Raises only CoordinationTransient."""
        counts = {"consumers": 0, "started": 0}
        if fact.get("cascade") == "none":
            return counts            # a cascade='none' backfill starts no consumer (ADR #57)
        try:
            fact_from_item(fact, declared_granularity="daily")
        except (KeyError, TypeError, ValueError) as exc:
            # The stream warned about it when it was recorded; a replay skips it with a log line (Principle #51).
            log.error("asset_fanout", "an unreadable fact starts no consumer", asset_name=str(fact.get("asset_name", "")),
                     event_time=str(fact.get("event_time", "")), error=str(exc))
            return counts
        asset = str(fact["asset_name"])
        for row in self._consumers(asset):
            if remaining_ms is not None and remaining_ms() < RECORD_RESERVE_MS:
                # The hourly catch-up replays this fact for the consumers not reached (ADR #130 §8).
                log.error("asset_fanout", "time budget reached inside a record; the catch-up starts the rest",
                          asset_name=asset, event_time=str(fact.get("event_time", "")))
                break
            consumer = str(row["pipeline_name"])
            if consumer == str(fact.get("source_dag", "")):
                continue             # a pipeline never triggers itself
            counts["consumers"] += 1
            try:
                counts["started"] += self._push_one(fact, row)
            except CoordinationTransient:
                raise
            except Exception as exc:  # noqa: BLE001 — one consumer's failure never fails the record (ADR #130 §6.1)
                reraise_transient(exc)
                self._warn(f"push:{consumer}", f"push consumer {consumer} failed: {type(exc).__name__}: {exc}")
        if counts["consumers"]:
            log.info("asset_fanout", "pushed", asset_name=asset, **counts)
        return counts

    def _push_one(self, fact: Dict[str, Any], row: Dict[str, Any]) -> int:
        """Start the consumer `row` names if the fact completes its schedule; 1 when this call started it."""
        asset, consumer, sfn_arn = str(fact["asset_name"]), str(row["pipeline_name"]), str(row["sfn_arn"])
        entry = self._registry_entry(consumer)
        # A consumer reacts only to facts recorded after it subscribed (B-U2). A row an older SDK wrote has
        # no subscribed_at, and the registered_at its runs rewrite would move: the pipeline's last deploy,
        # stamped in the registry by polyris-deploy, is the cutover instead.
        cutover = row.get("subscribed_at") or (entry or {}).get("registered_at")
        if cutover and parse_utc(str(fact["event_time"])) < parse_utc(str(cutover)):
            return 0
        registry = Registry(self.dispatcher.resource, self.dispatcher.pipelines_table)
        evidence = registry.fact(fact)
        schedule, scheduled = self._schedule(entry, consumer)
        if not scheduled or (schedule is not None and asset not in sp.asset_names(schedule)):
            # The pipeline was destroyed, switched to a cron schedule, or dropped this asset (B-U3).
            self._drop_subscription(row, "the pipeline is no longer scheduled on this asset")
            return 0
        if schedule is None:
            return 0             # unreadable: warned, and the row stays until the schedule is fixed
        every_event = fact.get("cascade") == "all" or schedule.operator == sp.OR
        if fact.get("cascade") != "all":
            reader = FactStore(self.dispatcher.resource, self.dispatcher.asset_events_table, registry,
                               self.dispatcher.clock(), on_unreadable=self.unreadable_fact)
            # The schedule is judged as it stood when the triggering fact arrived: `.within()` as of its time,
            # and no fact recorded after it, so a retry, a late record or the catch-up gives the same answer.
            evaluation = evaluate_assets(schedule, target=evidence.partition_date, now=evidence.event_time,
                                         reader=reader, known_facts=[evidence], recorded_by=evidence.event_time)
            if not evaluation.ready:
                return 0
        day = evidence.partition_date.isoformat()
        name = run_name(consumer, day, KIND_EVENT if every_event else KIND_AND,
                        f"{asset}|{fact['event_time']}" if every_event else "")
        run_input = json.dumps({
            "triggered_by": "asset",
            "trigger_assets": [asset] if every_event else sorted(sp.asset_names(schedule)),
            "source_dag": str(fact.get("source_dag", "")),
            "execution_date": day,
            # Top level as the console sends it, and under variables (ADR #133): a pipeline with variables=
            # replaces them in Define_Inputs, and one generated before 1.2 reads only the top-level date.
            "current_date": day,
            "PARTITION_ARG": day,
            "variables": {"current_date": day, "PARTITION_ARG": day},
        })
        try:
            self.dispatcher.sfn.start_execution(stateMachineArn=sfn_arn, name=name, input=run_input)
        except (ClientError, BotoCoreError) as exc:
            code = error_code(exc)
            if code == "ExecutionAlreadyExists":
                return 0             # started by an earlier event or a redelivery
            if is_transient(exc):
                self._retry_later(name, consumer, sfn_arn, run_input, f"{type(exc).__name__}: {exc}")
                return 0
            if code == "StateMachineDoesNotExist":
                self._drop_subscription(row, "its state machine no longer exists")
            self._warn(f"push:{name}", f"push consumer {consumer} could not be started: {type(exc).__name__}: {exc}")
            return 0
        return 1

    def _drop_subscription(self, row: Dict[str, Any], reason: str) -> None:
        """Delete a subscription its pipeline no longer has, unless the pipeline registered again meanwhile."""
        key = {"asset_name": row["asset_name"], "pipeline_name": row["pipeline_name"]}
        seen = row.get("registered_at")
        try:
            if seen is None:
                self.consumers.delete_item(Key=key, ConditionExpression="attribute_not_exists(registered_at)")
            else:
                self.consumers.delete_item(Key=key, ConditionExpression="registered_at = :seen",
                                           ExpressionAttributeValues={":seen": seen})
        except (ClientError, BotoCoreError) as exc:
            if is_condition_failed(exc):
                return              # registered again since we read it: it is current
            # Principle #41: the row only costs a skipped evaluation next time; log and go on.
            log.error("asset_fanout", "could not drop a stale subscription", asset_name=key["asset_name"],
                      pipeline_name=key["pipeline_name"], error=str(exc))
            return
        log.info("asset_fanout", "dropped a stale subscription", asset_name=key["asset_name"],
                 pipeline_name=key["pipeline_name"], reason=reason)

    def _registry_entry(self, consumer: str) -> Optional[Dict[str, Any]]:
        try:
            item: Optional[Dict[str, Any]] = self.registry_table.get_item(
                Key={"pipeline_name": consumer}, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            raise
        return item

    def _schedule(self, entry: Optional[Dict[str, Any]], consumer: str) -> Tuple[Optional[sp.AssetGroup], bool]:
        """The consumer's asset schedule, and whether it has one at all.

        ``(None, False)``: no registry entry, or ``"{}"``, which registration
        writes for a pipeline without an asset schedule: the pipeline is gone
        or runs on a cron. ``(None, True)``: a schedule that cannot be read, or
        none at all on a deployed pipeline, whose registration failed and
        warned then. Either is warned about, never acted on: dropping the row
        would lose a pipeline that never runs again to register.
        """
        if entry is None:
            return None, False
        raw = entry.get("asset_schedule")
        if raw is None:
            self._warn(f"push:{consumer}", f"push consumer {consumer} has no asset schedule in the registry, so "
                                            f"its registration did not finish: deploy it again")
            return None, True
        try:
            value = json.loads(raw) if isinstance(raw, str) else raw
            if not value:
                return None, False
            return sp.parse_schedule(value), True
        except ValueError as exc:
            self._warn(f"push:{consumer}", f"push consumer {consumer} has an unreadable asset schedule: {exc}")
            return None, True

    def _retry_later(self, name: str, consumer: str, sfn_arn: str, run_input: str, error: str) -> None:
        item = push_retry_item(run_name=name, consumer=consumer, sfn_arn=sfn_arn, run_input=run_input,
                               error=error, now=self.dispatcher.clock())
        try:
            # A row for the same run may exist already: keep it, with its first failure time.
            self.tasks.put_item(Item=item, ConditionExpression="attribute_not_exists(execution_name)")
        except (ClientError, BotoCoreError) as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return
            # Transient: the retry row is out of reach for now, so the record is redelivered (starting
            # nothing twice). Anything else would block the shard: report it; the sweeper's push
            # catch-up replays recent facts once the cause is fixed.
            reraise_transient(exc)
            self._warn(f"push:{name}", f"push consumer {consumer} could not be started ({error}) "
                                       f"and its retry row could not be written: {type(exc).__name__}: {exc}")
            return
        log.error("asset_fanout", "push start failed transiently; the sweeper retries", run_name=name, error=error)

    def _consumers(self, asset: str) -> List[Dict[str, Any]]:
        """Pipelines scheduled on `asset` (consistent, all pages)."""
        kwargs: Dict[str, Any] = {"KeyConditionExpression": Key("asset_name").eq(asset), "ConsistentRead": True}
        rows: List[Dict[str, Any]] = []
        try:
            while True:
                response = self.consumers.query(**kwargs)
                rows.extend(response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    return rows
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            self._warn(f"asset:{asset}", f"push consumers of asset {asset} could not be read: {type(exc).__name__}: {exc}")
            return []

    def _subscribers(self, asset: str) -> List[Tuple[str, Optional[str]]]:
        """The rows subscribed to `asset:{asset}`, with their runs' keys (consistent, all pages)."""
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key(start_run.POINTER_KEY).eq(f"asset:{asset}"),
            "ConsistentRead": True,
            "ProjectionExpression": "#subscriber, #run",
            "ExpressionAttributeNames": {"#subscriber": start_run.POINTER_SUBSCRIBER, "#run": start_run.POINTER_RUN},
        }
        found: List[Tuple[str, Optional[str]]] = []
        try:
            while True:
                response = self.subscriptions.query(**kwargs)
                found.extend((str(item[start_run.POINTER_SUBSCRIBER]), item.get(start_run.POINTER_RUN))
                             for item in response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    return found
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            # Non-transient (AccessDenied, a missing table): redelivery cannot fix it and would block the shard.
            self._warn(f"asset:{asset}", f"subscribers of asset {asset} could not be read: {type(exc).__name__}: {exc}")
            return []

    def unreadable_fact(self, item: Dict[str, Any], exc: Exception) -> None:
        """A fact no reader can use, met while a push consumer's schedule is judged: one warning per fact
        (repeated reads rewrite it), as the dispatcher writes for one met by a pass."""
        asset, recorded = str(item.get("asset_name", "")), str(item.get("event_time", ""))
        self._warn(f"fact-{asset}-{recorded}", f"The fact of asset {asset} recorded at {recorded} cannot be read, "
                                               f"so it counts for no task: {exc}")

    def _warn(self, subject: str, error: str) -> None:
        log.error("asset_fanout", "non-transient failure", subject=subject, error=error)
        kind = WarningKind.PUSH if subject.startswith("push:") else WarningKind.COORDINATION
        item = warning_item(kind=kind, execution_name=subject, task_name="",
                            pipeline_name="", pipeline_execution="", task_date="", error=error,
                            now=self.dispatcher.clock())
        try:
            self.tasks.put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            log.error("asset_fanout", "could not write the warning row", warning=item["execution_name"], error=str(exc))


_FANOUT: Optional[AssetFanout] = None


def _fanout() -> AssetFanout:
    global _FANOUT
    if _FANOUT is None:
        import dispatcher

        _FANOUT = AssetFanout(dispatcher._dispatcher())
    return _FANOUT


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    remaining: Callable[[], int] = context.get_remaining_time_in_millis if context else (lambda: 10 ** 9)
    return _fanout().handle(event.get("Records", []), remaining)
