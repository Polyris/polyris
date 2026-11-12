"""AssetFanout: wake the tasks that wait on a new asset fact (ADR #130 §6.1).

An event source mapping feeds it the asset-events stream (INSERT records
only). For each new fact it asks the reconciler about every task subscribed
to `asset:{name}`, passing the fact itself as a known event, so the decision
does not depend on how quickly the partition index catches up.

It also starts the pipelines scheduled on the asset (push): each consumer's
whole `schedule=[...]` is evaluated for the fact's own partition with the
same rule as pull, `.within()` as of the fact's time, and a satisfied
consumer is started under a deterministic name — one run per date for an
AND schedule, one per event for OR or `cascade='all'` — so a repeated
record never starts a second run.

Pull wakes are hints: a reconcile call that fails is logged and left to the
sweeper. A failure for one consumer never fails the record: a transient
start failure becomes a push retry row for the sweeper, anything else a
warning. A record is reported back for redelivery only when its subscribers
could not be read (transiently), when even a retry row could not be
written, or when the time budget ran out before it was reached; redelivery
is safe because every action is idempotent.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import BotoCoreError, ClientError

import env
from coordination_core import spec as sp
from coordination_core.asset_conditions import evaluate as evaluate_assets
from coordination_core.records import fact_from_item, push_retry_item, warning_item
from coordination_core.run_name import KIND_AND, KIND_EVENT, run_name
from failures import CoordinationTransient, error_code, is_transient, reraise_transient
from logger import log
from reconcile import Reconciler
from stores import FactStore, Registry

#: Stop starting new records with this much of the invocation left.
RESERVE_MS = 10_000
_deserializer = TypeDeserializer()


def fact_of(record: Dict[str, Any]) -> Dict[str, Any]:
    """The asset-events item a stream record carries."""
    image = record["dynamodb"]["NewImage"]
    return {key: _deserializer.deserialize(value) for key, value in image.items()}


class AssetFanout:
    def __init__(self, reconciler: Reconciler, *, subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
                 consumers_table: str = env.ASSET_SUBSCRIPTIONS_TABLE):
        self.reconciler = reconciler
        self.subscriptions = reconciler.resource.Table(subscriptions_table)
        self.consumers = reconciler.resource.Table(consumers_table)
        self.registry_table = reconciler.resource.Table(reconciler.pipelines_table)

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
                fact_from_item(fact, declared_granularity="daily")     # a fact nobody could read is poison
                self.fan_out(fact)
                self.push(fact)
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

    def fan_out(self, fact: Dict[str, Any]) -> Dict[str, int]:
        """Reconcile every task waiting on the fact's asset. Raises only CoordinationTransient."""
        asset = str(fact["asset_name"])
        subscribers = self._subscribers(asset)
        woken = lost = 0
        for name in subscribers:
            try:
                result = self.reconciler.reconcile(name, source="asset_fanout", known_events=[fact])
                woken += result["action"] == "signalled"
            except CoordinationTransient as exc:
                lost += 1
                # Principle #41: a lost hint delays the task; the sweeper re-checks it from facts.
                log.error("asset_fanout", "reconcile failed transiently; the sweeper re-checks the task",
                          execution_name=name, asset_name=asset, error=str(exc))
        counts = {"subscribers": len(subscribers), "woken": woken, "lost": lost}
        log.info("asset_fanout", "fanned out", asset_name=asset,
                 partition_key=str(fact.get("partition_key", fact.get("execution_date", ""))), **counts)
        return counts

    def push(self, fact: Dict[str, Any]) -> Dict[str, int]:
        """Start every push consumer the fact completes. Raises only CoordinationTransient."""
        counts = {"consumers": 0, "started": 0}
        if fact.get("cascade") == "none":
            return counts            # a cascade='none' backfill starts no consumer (ADR #57)
        asset = str(fact["asset_name"])
        for row in self._consumers(asset):
            consumer = str(row["pipeline_name"])
            if consumer == str(fact.get("source_dag", "")):
                continue             # a pipeline never triggers itself
            counts["consumers"] += 1
            try:
                counts["started"] += self._push_one(fact, consumer, str(row["sfn_arn"]))
            except CoordinationTransient:
                raise
            except Exception as exc:  # noqa: BLE001 — one consumer's failure never fails the record (ADR #130 §6.1)
                reraise_transient(exc)
                self._warn(f"push:{consumer}", f"push consumer {consumer} failed: {type(exc).__name__}: {exc}")
        if counts["consumers"]:
            log.info("asset_fanout", "pushed", asset_name=asset, **counts)
        return counts

    def _push_one(self, fact: Dict[str, Any], consumer: str, sfn_arn: str) -> int:
        """Start `consumer` if the fact completes its schedule; 1 when this call started it."""
        asset = str(fact["asset_name"])
        registry = Registry(self.reconciler.resource, self.reconciler.pipelines_table)
        evidence = registry.fact(fact)
        schedule = self._schedule(consumer)
        if schedule is None:
            self._warn(f"push:{consumer}", f"push consumer {consumer} has no asset schedule in the registry")
            return 0
        every_event = fact.get("cascade") == "all" or schedule.operator == sp.OR
        if fact.get("cascade") != "all":
            reader = FactStore(self.reconciler.resource, self.reconciler.asset_events_table, registry,
                               self.reconciler.clock())
            # `.within()` is judged as of the triggering fact, so a retry or a late record gives the same answer.
            evaluation = evaluate_assets(schedule, target=evidence.partition_date, now=evidence.event_time,
                                         reader=reader, known_facts=[evidence])
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
            "variables": {"current_date": day, "PARTITION_ARG": day},
        })
        try:
            self.reconciler.signals.client.start_execution(stateMachineArn=sfn_arn, name=name, input=run_input)
        except (ClientError, BotoCoreError) as exc:
            code = error_code(exc)
            if code == "ExecutionAlreadyExists":
                return 0             # started by an earlier event or a redelivery
            if is_transient(exc):
                self._retry_later(name, consumer, sfn_arn, run_input, f"{type(exc).__name__}: {exc}")
                return 0
            self._warn(f"push:{name}", f"push consumer {consumer} could not be started: {type(exc).__name__}: {exc}")
            return 0
        return 1

    def _schedule(self, consumer: str) -> Optional[sp.AssetGroup]:
        try:
            item = self.registry_table.get_item(Key={"pipeline_name": consumer}, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            raise
        raw = (item or {}).get("asset_schedule")
        if not raw:
            return None
        try:
            return sp.parse_schedule(json.loads(raw) if isinstance(raw, str) else raw)
        except ValueError as exc:
            self._warn(f"push:{consumer}", f"push consumer {consumer} has an unreadable asset schedule: {exc}")
            return None

    def _retry_later(self, name: str, consumer: str, sfn_arn: str, run_input: str, error: str) -> None:
        item = push_retry_item(run_name=name, consumer=consumer, sfn_arn=sfn_arn, run_input=run_input,
                               error=error, now=self.reconciler.clock())
        try:
            # A row for the same run may exist already: keep it, with its first failure time.
            self.reconciler.tasks.table.put_item(Item=item, ConditionExpression="attribute_not_exists(execution_name)")
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

    def _subscribers(self, asset: str) -> List[str]:
        """execution_names subscribed to `asset:{asset}` (consistent, all pages)."""
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("dependency_key").eq(f"asset:{asset}"),
            "ConsistentRead": True,
            "ProjectionExpression": "subscriber",
        }
        names: List[str] = []
        try:
            while True:
                response = self.subscriptions.query(**kwargs)
                names.extend(str(item["subscriber"]) for item in response.get("Items", []))
                if "LastEvaluatedKey" not in response:
                    return names
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        except (ClientError, BotoCoreError) as exc:
            reraise_transient(exc)
            # Non-transient (AccessDenied, a missing table): redelivery cannot fix it and would block the shard.
            self._warn(f"asset:{asset}", f"subscribers of asset {asset} could not be read: {type(exc).__name__}: {exc}")
            return []

    def _warn(self, subject: str, error: str) -> None:
        log.error("asset_fanout", "non-transient failure", subject=subject, error=error)
        kind = "push" if subject.startswith("push:") else "coordination"
        item = warning_item(kind=kind, execution_name=subject, task_name="",
                            pipeline_name="", pipeline_execution="", task_date="", error=error,
                            now=self.reconciler.clock())
        try:
            self.reconciler.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("asset_fanout", "could not write the warning row", warning=item["execution_name"], error=str(exc))


_FANOUT: Optional[AssetFanout] = None


def _fanout() -> AssetFanout:
    global _FANOUT
    if _FANOUT is None:
        _FANOUT = AssetFanout(Reconciler(env.dynamodb(), env.stepfunctions()))
    return _FANOUT


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    remaining: Callable[[], int] = context.get_remaining_time_in_millis if context else (lambda: 10 ** 9)
    return _fanout().handle(event.get("Records", []), remaining)
