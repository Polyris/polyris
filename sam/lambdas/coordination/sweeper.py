"""The sweeper: the periodic check behind every wake-up hint (ADR #130 §8, ADR #137 §6, §7).

An EventBridge schedule runs it every 5 minutes. One run at a time holds a
lease; a run that cannot take it exits. Within its budget it:
    * takes the due entries of the ``waiting`` and ``decision`` kinds of the
      attention index, run rows included, earliest first, and makes one full
      pass of the dispatcher over each of their runs, in its own process. The
      pass judges every row of the run from the row itself; the sweeper's
      passes also judge the executions of the started rows and of the run,
      and the engine stops a run whose execution ended before its answer or
      that never became active (§4). An execution's end on EventBridge makes
      that check come at once for its own execution; a lost event leaves it
      to this one (§7). The rows of a run whose run row is
      missing are stopped (invariant 11); a row a 1.2 run left in
      flight is skipped and leaves the attention index, and expires;
    * after each check moves the run row's due time on by the backoff (at most
      30 minutes) and clears its ``left_at``, a failed pass included, so one
      broken run holds neither the budget nor the System status; the
      dispatcher warns about the failed pass, and a sweep whose passes mostly
      failed reports a failed step;
    * writes one ``sweeper`` warning per run whose check found lost hints;
    * retries the push starts that failed transiently, and once an hour
      replays the push branch for the facts of the last 48 hours, so a lost
      stream record cannot lose a push start either;
    * writes its self-check into ``_engine_health``, which the console's
      System status reads (``sweep.engine_findings``).
A hint lost by any primary path therefore delays a task; it never loses it
(Principle #41).
"""

import heapq
import os
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from polyris.constants import WarningKind
from polyris.coordination import attention as att
from polyris.coordination import dispatch_writes, run_row, sweep, task_row
from polyris.coordination.records import warning_item
from polyris.coordination.times import format_utc, parse_utc
import env
import dispatcher as dispatching
from asset_fanout import AssetFanout
from failures import CoordinationTransient, error_code, is_condition_failed, is_transient
from logger import log

#: A failed step's error is cut to this length in `_engine_health`; its log line keeps all of it.
_FAILURE_CHARS = 500
#: The due entries the sweeper reads: rows with a time of their own and the run rows, and decision deadlines (§6).
DUE_KINDS = (att.WAITING, att.DECISION)
#: What `_check` answers for a run whose last pass failed for good and whose run row is not due yet: none is made.
BACKOFF = "backoff"


class Sweeper:
    def __init__(self, dispatcher: dispatching.Dispatcher, *, owner: Optional[str] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
                 consumers_table: str = env.ASSET_SUBSCRIPTIONS_TABLE,
                 lambda_client: Any = None, fanout_function: str = env.ASSET_FANOUT_FUNCTION,
                 dispatcher_function: str = env.DISPATCHER_FUNCTION):
        self.dispatcher = dispatcher
        self.clock = dispatcher.clock
        self.lambda_client = lambda_client
        self.fanout_function = fanout_function
        self.dispatcher_function = dispatcher_function
        self.owner = owner or uuid.uuid4().hex
        self.monotonic = monotonic
        resource = dispatcher.resource
        self.tasks = resource.Table(dispatcher.tokens_table)
        self.fanout = AssetFanout(dispatcher, subscriptions_table=subscriptions_table, consumers_table=consumers_table)
        self.consumers = resource.Table(consumers_table)
        self.facts = resource.Table(dispatcher.asset_events_table)

    def run(self) -> Dict[str, Any]:
        now = self.clock()
        if not self._take_lease(now):
            log.info("sweeper", "another run holds the lease")
            return {"skipped": "lease held"}
        deadline = self.monotonic() + sweep.BUDGET.total_seconds()
        result: Dict[str, Any] = {}
        failed: List[str] = []
        try:
            for step in sweep.STEPS:
                try:
                    counts = getattr(self, step)(deadline=deadline)
                except Exception as exc:  # noqa: BLE001 — Principle #38: the failure reaches the console's findings
                    # One failing step must not skip the others or the self-check; the next run tries it again.
                    failed.append(f"{step}: {type(exc).__name__}: {exc}"[:_FAILURE_CHARS])
                    log.error("sweeper", "a step failed; the next run tries it again", step=step, error=str(exc))
                    continue
                failure = counts.pop("failed_step", None)
                if failure:
                    failed.append(f"{step}: {failure}"[:_FAILURE_CHARS])
                result.update(counts)
            result.update(self.self_check(failed_steps=failed, lost_hint_runs=int(result.get("lost_hint_runs", 0))))
        finally:
            self._release_lease()
        log.info("sweeper", "done", **result)
        return result

    # ── lease ───────────────────────────────────────────────────────────

    def _take_lease(self, now: datetime) -> bool:
        try:
            self.tasks.put_item(
                Item={"execution_name": sweep.LEASE_KEY, "owner": self.owner,
                      "expires_at": format_utc(now + sweep.LEASE)},
                # A resend of our own put, whose answer was lost, must not fail its own condition.
                ConditionExpression="attribute_not_exists(execution_name) OR expires_at < :now OR #o = :me",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":now": format_utc(now), ":me": self.owner},
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False
            raise
        return True

    def _release_lease(self) -> None:
        try:
            self.tasks.delete_item(Key={"execution_name": sweep.LEASE_KEY},
                                   ConditionExpression="#o = :me",
                                   ExpressionAttributeNames={"#o": "owner"},
                                   ExpressionAttributeValues={":me": self.owner})
        except (ClientError, BotoCoreError) as exc:
            # The lease expires on its own; the next run takes it then.
            log.warn("sweeper", "could not release the lease", error=str(exc))

    # ── the runs that are due (ADR #137 §7) ─────────────────────────────

    def due_runs(self, *, deadline: float) -> Dict[str, Any]:
        """One full pass over the run of each due entry, earliest first, until `deadline` (monotonic)."""
        now = self.clock()
        counts: Dict[str, Any] = {"runs_checked": 0, "passes_failed": 0, "lost_hint_runs": 0, "orphans_stopped": 0,
                                  "budget_reached": False}
        describes = sweep.Calls(sweep.DESCRIBES)       # shared by every run this sweep checks (S5)
        checked: Set[str] = set()
        orphaned: Set[str] = set()
        first_failure = ""
        for entry in self._due(now):
            if self.monotonic() >= deadline - dispatching.RESERVE_MS / 1000:
                counts["budget_reached"] = True     # the rest stay due: the next run starts with them
                break
            key = str(entry[run_row.TABLE_KEY])
            run_key = self._run_of(entry)
            if run_key is None:
                # Nothing writes such an entry; it stays due, and the self-check's oldest overdue row shows it.
                log.error("sweeper", "a due entry names no run", execution_name=key)
                continue
            if run_key in orphaned:
                counts["orphans_stopped"] += self._stop_orphan(run_key, key)
                continue
            if run_key in checked:
                continue
            checked.add(run_key)
            result, failure = self._check(run_key, key, now, deadline, describes=describes)
            if result.get("action") == BACKOFF:
                continue
            counts["runs_checked"] += 1
            if failure:
                counts["passes_failed"] += 1
                first_failure = first_failure or failure
            elif result.get("action") == "orphans":
                orphaned.add(run_key)
                counts["orphans_stopped"] += int(result.get("stopped", 0))
            elif result.get("lost"):
                counts["lost_hint_runs"] += 1
        counts["describes"] = sweep.DESCRIBES - describes.left
        if sweep.failed_step(counts["runs_checked"], counts["passes_failed"]):
            counts["failed_step"] = sweep.failed_passes(counts["runs_checked"], counts["passes_failed"], first_failure)
        return counts

    def _check(self, run_key: str, key: str, now: datetime, deadline: float, *,
               describes: sweep.Calls) -> Tuple[Dict[str, Any], str]:
        """The check of one run: its full pass, then the move of its due time, and its lost-hint warning.

        Returns the pass's result and, when the pass failed, the cause. The
        dispatcher warns about a pass that failed for good; a transient failure
        is the next sweep's to retry.
        """
        try:
            run_item = self.tasks.get_item(Key={run_row.TABLE_KEY: run_key}, ConsistentRead=True).get("Item")
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "the run row could not be read; the run stays due", run=run_key, error=str(exc))
            return {}, f"{type(exc).__name__}: {exc}"
        if run_item and run_item.get(run_row.CHECK_FAILED) and str(run_item.get(att.DUE) or "") > format_utc(now):
            return {"action": BACKOFF}, ""      # its last pass failed for good: it waits for its run row's due time

        def remaining() -> int:
            return max(0, int((deadline - self.monotonic()) * 1000))

        failure = ""
        try:
            result = self.dispatcher.full_pass(run_key, source=sweep.SOURCE, remaining_ms=remaining, check=True,
                                               describes=describes, orphan_keys=[key])
        except CoordinationTransient as exc:
            log.error("sweeper", "a pass failed transiently; the next sweep checks the run again", run=run_key,
                      error=str(exc))
            # Retried at the run's next check, and counted toward the sweep's failed step (Principle #38).
            result, failure = {}, f"CoordinationTransient: {exc}"
        else:
            if result.get("action") == "failed":
                failure = str(result.get("error", ""))
        if run_item is not None:
            self._move(run_key, run_item, now, failed=result.get("action") == "failed")
            if result.get("lost"):
                self._lost_hints(run_key, run_item, result["lost"])
        return result, failure

    def _stop_orphan(self, run_key: str, key: str) -> int:
        """A further row of a run whose run row is missing, stopped; one that fails stays due for the next sweep."""
        try:
            return int(self.dispatcher.stop_orphans(run_key, [key], source=sweep.SOURCE)["stopped"])
        except (CoordinationTransient, ClientError, BotoCoreError) as exc:
            log.error("sweeper", "a row whose run row is missing could not be stopped; it stays due",
                      execution_name=key, error=str(exc))
            return 0

    def _due(self, now: datetime, kinds: Sequence[str] = DUE_KINDS) -> Iterator[Dict[str, Any]]:
        """The due entries of every shard of `kinds`, merged into one stream ordered by due time."""
        shards = [self._shard(partition, now) for kind in kinds for partition in att.partitions(kind)]
        return heapq.merge(*shards, key=lambda row: str(row[att.DUE]))

    def _shard(self, partition: str, now: datetime) -> Iterator[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "IndexName": att.INDEX,
            "KeyConditionExpression": Key(att.KEY).eq(partition) & Key(att.DUE).lte(format_utc(now)),
        }
        while True:
            response = self.tasks.query(**kwargs)
            yield from response.get("Items", [])
            if "LastEvaluatedKey" not in response:
                return
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    @staticmethod
    def _run_of(entry: Dict[str, Any]) -> Optional[str]:
        """The run row's key of a due entry: the run row itself, or the run of a task row."""
        key = str(entry[run_row.TABLE_KEY])
        if run_row.is_run_row(key):
            return key
        execution = entry.get(task_row.PIPELINE_EXECUTION)
        return run_row.key(str(execution)) if execution else None

    def _move(self, run_key: str, run_item: Dict[str, Any], now: datetime, *, failed: bool) -> None:
        """The run row's due time moved on by the backoff and its `left_at` cleared, when the run row itself was due,
        on the due time and `run_version` read before the pass (§3). A pass that failed for good marks the run row,
        due or not, so the sweeper leaves the run to that backoff (§7)."""
        due, version = run_item.get(att.DUE), run_item.get(run_row.RUN_VERSION)
        if not due or version is None:
            return
        table = self.dispatcher.tokens_table
        if str(due) <= format_utc(now):
            request = dispatch_writes.checked(run_key, table=table, due_read=str(due), version_read=int(version),
                                              checks=int(run_item.get(run_row.CHECKS, 0)), now=now, failed=failed)
        elif failed:
            request = dispatch_writes.check_failed(run_key, table=table, due_read=str(due), version_read=int(version),
                                                   now=now)
        else:
            return
        try:
            self.dispatcher.client.update_item(**request)
        except ClientError as exc:
            if is_condition_failed(exc):
                return                  # a pass, a request or an action set its due time, or raised run_version
            log.error("sweeper", "could not move the run row's due time; the next sweep checks it again",
                      run=run_key, error=str(exc))
        except BotoCoreError as exc:
            log.error("sweeper", "could not move the run row's due time; the next sweep checks it again",
                      run=run_key, error=str(exc))

    def _lost_hints(self, run_key: str, run_item: Dict[str, Any], tasks: Sequence[str]) -> None:
        """One `sweeper` warning for the run, keyed by the run: a later check that finds more replaces it (§7)."""
        self._put_warning(warning_item(
            kind=WarningKind.SWEEPER, execution_name=f"{run_key}-lost-hints", task_name="",
            pipeline_name=str(run_item.get(run_row.PIPELINE, "")),
            pipeline_execution=run_key[len(run_row.KEY_PREFIX):], task_date=str(run_item.get(run_row.RUN_DATE, "")),
            error=sweep.lost_hints_warning(tasks), now=self.clock()))

    def _put_warning(self, item: Dict[str, Any]) -> None:
        try:
            self.tasks.put_item(Item=item)
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "could not write the warning row", warning=item["execution_name"], error=str(exc))

    # ── push retries ────────────────────────────────────────────────────

    def push_retries(self, *, deadline: float) -> Dict[str, Any]:
        """Start again the push runs whose start failed transiently (ADR #130 §7, §8)."""
        now = self.clock()
        counts = {"push_retried": 0, "push_started": 0, "push_given_up": 0}
        for listed in self._due(now, (att.RETRY,)):
            if self.monotonic() >= deadline:
                break
            # The index only finds candidates; the row itself is read from the table (ADR #130 §3).
            row = self._get(str(listed["execution_name"]))
            if row is None:
                continue                # done meanwhile
            counts["push_retried"] += 1
            try:
                consumer, name = str(row.get("consumer", "")), str(row["run_name"])
                first_failed_at = parse_utc(str(row["first_failed_at"]))
                sfn_arn, run_input = str(row["sfn_arn"]), str(row["run_input"])
                attempts = int(row.get("attempts", 0))
            except (KeyError, TypeError, ValueError) as exc:
                # One unreadable row must not stop the retries behind it, run after run (Principle #51).
                self._unreadable_retry(row, exc)
                counts["push_given_up"] += 1
                continue
            if now - first_failed_at >= sweep.PUSH_RETRY_WINDOW:
                self._push_warning(row, f"push consumer {consumer} could not be started for 24 hours, "
                                        f"so the sweeper gave up; last error: {row.get('error', '')}")
                self._drop(row)
                counts["push_given_up"] += 1
                continue
            try:
                self.dispatcher.sfn.start_execution(stateMachineArn=sfn_arn, name=name, input=run_input)
            except (ClientError, BotoCoreError) as exc:
                if error_code(exc) != "ExecutionAlreadyExists":
                    if is_transient(exc):
                        self._retry_again(row, now, attempts, f"{type(exc).__name__}: {exc}")
                    else:
                        self._push_warning(row, f"push consumer {consumer} could not be started: "
                                                f"{type(exc).__name__}: {exc}")
                        self._drop(row)
                    continue
            counts["push_started"] += 1
            self._drop(row)
        return counts

    def _get(self, key: str) -> Optional[Dict[str, Any]]:
        item: Optional[Dict[str, Any]] = self.tasks.get_item(Key={"execution_name": key}, ConsistentRead=True).get("Item")
        return item

    def _retry_again(self, row: Dict[str, Any], now: datetime, attempts: int, error: str) -> None:
        attempts += 1
        try:
            self.tasks.update_item(
                Key={"execution_name": row["execution_name"]},
                UpdateExpression="SET #due = :due, attempts = :n, #e = :e",
                ConditionExpression="attribute_exists(execution_name)",
                ExpressionAttributeNames={"#due": att.DUE, "#e": "error"},
                ExpressionAttributeValues={":due": format_utc(sweep.next_push_retry(now, attempts)),
                                           ":n": attempts, ":e": error},
            )
        except (ClientError, BotoCoreError) as exc:
            # The row stays due; the next run tries the start again.
            log.error("sweeper", "could not set the next push retry", run_name=row["run_name"], error=str(exc))

    def _drop(self, row: Dict[str, Any]) -> None:
        try:
            self.tasks.delete_item(Key={"execution_name": row["execution_name"]})
        except (ClientError, BotoCoreError) as exc:
            # A leftover row is retried again: its start is idempotent (deterministic name).
            log.error("sweeper", "could not delete the push retry row", execution_name=row["execution_name"],
                      error=str(exc))

    def _unreadable_retry(self, row: Dict[str, Any], exc: Exception) -> None:
        """Drop a push retry row that cannot be read, and say so: it can never be retried."""
        name = str(row["execution_name"])
        log.error("sweeper", "a push retry row cannot be read; dropped", execution_name=name, error=str(exc))
        self._put_warning(warning_item(
            kind=WarningKind.PUSH, execution_name=f"push:{name}", task_name="",
            pipeline_name=str(row.get("consumer", "")), pipeline_execution="", task_date="", now=self.clock(),
            error=f"A failed push start ({name}) cannot be retried, because its retry row cannot be read: "
                  f"{type(exc).__name__}: {exc}. Start the pipeline from the console if it should run."))
        self._drop(row)

    def _push_warning(self, row: Dict[str, Any], error: str) -> None:
        self._put_warning(warning_item(kind=WarningKind.PUSH, execution_name=f"push:{row['run_name']}", task_name="",
                                       pipeline_name=str(row.get("consumer", "")), pipeline_execution="",
                                       task_date="", error=error, now=self.clock()))

    # ── catch-up ────────────────────────────────────────────────────────

    def catch_up(self, *, deadline: float) -> Dict[str, Any]:
        """Hourly: replay AssetFanout's push branch for the facts of the last 48 hours (ADR #130 §8).

        Runs start under deterministic names, so a run that already started
        is only an ExecutionAlreadyExists. Only facts the ADR #130 writers wrote
        are replayed. A run that reaches its deadline keeps a cursor, and the
        next run goes on.
        """
        now = self.clock()
        health = self._get(sweep.HEALTH_KEY) or {}
        cursor = str(health.get("catchup_cursor", ""))            # the last asset finished
        within = health.get("catchup_within") or {}                # how far the next asset got
        last = parse_utc(str(health["catchup_at"])) if health.get("catchup_at") else None
        if not cursor and not within and not sweep.catch_up_due(now, last):
            return {"catch_up": "not due"}
        since = format_utc(now - sweep.CATCH_UP_WINDOW)
        replayed = 0
        for asset in sorted(self._subscribed_assets()):
            if asset <= cursor:
                continue
            if self.monotonic() >= deadline:
                self._save_catch_up(cursor=cursor, within=within or None)
                return {"catch_up": "partial", "facts_replayed": replayed}
            after = str(within.get("after", "")) if within.get("asset") == asset else ""
            for fact in self._recent_facts(asset, since):
                event_time = str(fact["event_time"])
                if event_time <= after:
                    continue                                   # replayed by the run that stopped here
                if self.monotonic() >= deadline:
                    self._save_catch_up(cursor=cursor, within={"asset": asset, "after": after} if after else None)
                    return {"catch_up": "partial", "facts_replayed": replayed}
                self.fanout.push(fact)
                replayed += 1
                after = event_time
            cursor = asset
        self._save_catch_up(done_at=now)
        return {"catch_up": "done", "facts_replayed": replayed}

    def _subscribed_assets(self) -> set:
        kwargs: Dict[str, Any] = {"ProjectionExpression": "asset_name", "ConsistentRead": True}
        assets: set = set()
        while True:
            response = self.consumers.scan(**kwargs)
            assets.update(str(item["asset_name"]) for item in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                return assets
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _recent_facts(self, asset: str, since: str) -> Iterator[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "KeyConditionExpression": Key("asset_name").eq(asset) & Key("event_time").gte(since),
            "ConsistentRead": True,
        }
        while True:
            response = self.facts.query(**kwargs)
            yield from response.get("Items", [])
            if "LastEvaluatedKey" not in response:
                return
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _save_catch_up(self, *, cursor: str = "", within: Optional[Dict[str, str]] = None,
                       done_at: Optional[datetime] = None) -> None:
        values: Dict[str, Any]
        if done_at is not None:
            update, values = "SET catchup_at = :at REMOVE catchup_cursor, catchup_within", {":at": format_utc(done_at)}
        elif within:
            update, values = "SET catchup_cursor = :c, catchup_within = :w", {":c": cursor, ":w": within}
        else:
            update, values = "SET catchup_cursor = :c REMOVE catchup_within", {":c": cursor}
        try:
            self.tasks.update_item(Key={"execution_name": sweep.HEALTH_KEY}, UpdateExpression=update,
                                   ExpressionAttributeValues=values)
        except (ClientError, BotoCoreError) as exc:
            # The next run starts the catch-up over: replaying is idempotent.
            log.error("sweeper", "could not save the catch-up progress", error=str(exc))

    # ── self-check ──────────────────────────────────────────────────────

    def self_check(self, failed_steps: Sequence[str] = (), lost_hint_runs: int = 0) -> Dict[str, Any]:
        """Write what the console's health check reads into `_engine_health` (ADR #130 §9, ADR #137 §6)."""
        now = self.clock()
        failed = list(failed_steps)
        fanout = self._mapping(self.fanout_function)
        tasks = self._mapping(self.dispatcher_function)
        report = {
            "checked_at": format_utc(now),
            "fanout_mapping": fanout["state"],
            "fanout_result": fanout["result"],
            # What the console names in its details, and when the mapping last changed state: the
            # time it was disabled, kept by AWS, so "since" needs no state of our own.
            "fanout_mapping_uuid": fanout["uuid"],
            "fanout_mapping_changed_at": fanout["changed_at"],
            "fanout_function": self.fanout_function,
            "dispatch_mapping": tasks["state"],
            "dispatch_result": tasks["result"],
            "dispatch_mapping_uuid": tasks["uuid"],
            "dispatch_mapping_changed_at": tasks["changed_at"],
            "dispatcher_function": self.dispatcher_function,
            "sweeper_function": os.environ.get("AWS_LAMBDA_FUNCTION_NAME", ""),
            "events_table": self.dispatcher.asset_events_table,
            "tasks_table": self.dispatcher.tokens_table,
            # The region the console's commands pass as --region; the Lambda runtime sets it.
            "region": os.environ.get("AWS_REGION", ""),
            "stream": self._stream_state(self.dispatcher.asset_events_table),
            "tasks_stream": self._stream_state(self.dispatcher.tokens_table),
            # A shard held by a record that keeps failing shows here, where LastProcessingResult may not (§6).
            "lost_hint_runs": lost_hint_runs,
            "oldest_overdue": self._reading("the oldest overdue task", lambda: self._oldest_due(now), "", failed),
            "push_retry_backlog": self._reading("the push retry backlog", self._backlog, 0, failed),
            "failed_steps": failed,
        }
        self._save_health(report)
        findings = sweep.engine_findings(report, now)
        if findings:
            log.error("sweeper", "the coordination engine is unhealthy", findings=[f["title"] for f in findings])
        return {"healthy": not findings}

    @staticmethod
    def _reading(name: str, read: Callable[[], Any], neutral: Any, failed: List[str]) -> Any:
        """One reading of the self-check. A failed one goes to `failed_steps`, so the health row
        is still written and the console's findings name it."""
        try:
            return read()
        except (ClientError, BotoCoreError) as exc:
            failed.append(f"{sweep.SELF_CHECK}: {name} could not be read: {type(exc).__name__}: {exc}"[:_FAILURE_CHARS])
            log.error("sweeper", "a self-check reading failed", reading=name, error=str(exc))
            return neutral

    def _mapping(self, function: str) -> Dict[str, str]:
        """A function's stream mapping: its State, LastProcessingResult, UUID, and when it last changed
        (LastModified, which AWS moves when the mapping is disabled or enabled)."""
        client = self.lambda_client or env.lambda_()
        found = {"state": sweep.MAPPING_MISSING, "result": "", "uuid": "", "changed_at": ""}
        try:
            mappings = client.list_event_source_mappings(FunctionName=function)["EventSourceMappings"]
        except (ClientError, BotoCoreError) as exc:
            return {**found, "state": sweep.unknown_state(error_code(exc) or type(exc).__name__)}
        if not mappings:
            return found
        mapping = mappings[0]
        changed = mapping.get("LastModified")
        return {"state": str(mapping.get("State") or sweep.unknown_state("no State")),
                "result": str(mapping.get("LastProcessingResult", "")), "uuid": str(mapping.get("UUID", "")),
                "changed_at": format_utc(changed) if isinstance(changed, datetime) else ""}

    def _stream_state(self, table: str) -> str:
        try:
            described = self.dispatcher.client.describe_table(TableName=table)["Table"]
        except (ClientError, BotoCoreError) as exc:
            return sweep.unknown_state(error_code(exc) or type(exc).__name__)
        return sweep.STREAM_ENABLED if described.get("StreamSpecification", {}).get("StreamEnabled") else "DISABLED"

    def _oldest_due(self, now: datetime) -> str:
        """The earliest due time among the entries the sweeper checks, run rows included, still due after this run."""
        oldest = ""
        for partition in (p for kind in DUE_KINDS for p in att.partitions(kind)):
            items = self.tasks.query(
                IndexName=att.INDEX, Limit=1,
                KeyConditionExpression=Key(att.KEY).eq(partition) & Key(att.DUE).lte(format_utc(now)),
            ).get("Items", [])
            if items and (not oldest or str(items[0][att.DUE]) < oldest):
                oldest = str(items[0][att.DUE])
        return oldest

    def _backlog(self) -> int:
        total = 0
        for partition in att.partitions(att.RETRY):
            kwargs: Dict[str, Any] = {"IndexName": att.INDEX, "Select": "COUNT",
                                      "KeyConditionExpression": Key(att.KEY).eq(partition)}
            while True:
                response = self.tasks.query(**kwargs)
                total += int(response.get("Count", 0))
                if "LastEvaluatedKey" not in response:
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return total

    def _save_health(self, fields: Dict[str, Any]) -> None:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        try:
            self.tasks.update_item(
                Key={"execution_name": sweep.HEALTH_KEY},
                UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
                ExpressionAttributeNames=names, ExpressionAttributeValues=values,
            )
        except (ClientError, BotoCoreError) as exc:
            # The console then sees a stale report and shows the engine as unhealthy.
            log.error("sweeper", "could not write the engine health row", error=str(exc))


_SWEEPER: Optional[Sweeper] = None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    owner = getattr(context, "aws_request_id", None) or uuid.uuid4().hex
    sweeper = _SWEEPER or Sweeper(dispatching._dispatcher(), owner=owner)
    return sweeper.run()
