"""The sweeper: the periodic re-check from facts behind every wake-up hint (ADR #130 §8).

An EventBridge schedule runs it every 5 minutes. One run at a time holds a
lease; a run that cannot take it exits. Within its budget it takes the
waiting rows whose due time has come, earliest first, and asks the
reconciler about each, in-process:
    * a task that is still waiting is due again later (10 → 20 → 30 minutes,
      then every 30), so nothing starves and the work per task is bounded;
    * a task the sweeper wakes although it could run before its due time had
      its hint lost on the primary path: that is written as a warning;
    * past its liveness deadline, the reconciler also checks the task's wrapper
      and aborts a task nothing can wake any more (ADR #130 §3).
A hint lost by any primary path therefore delays a task; it never loses it
(Principle #41). The same run releases the tasks held in their pause wait
whose run is no longer paused, which a resume can miss; retries the push
starts that failed transiently; and once an hour replays the push branch for
the facts of the last 48 hours, so a lost stream record cannot lose a push
start either.
"""

import heapq
import json
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from coordination_core import attention as att
from coordination_core import decide as dc
from coordination_core import sweep
from coordination_core.partition import partition_date
from coordination_core.records import FACT_VERSION, fact_from_item, warning_item
from coordination_core.statuses import RECONCILABLE, WAITING_PAUSED
from coordination_core.times import format_utc, parse_utc
import env
from asset_fanout import AssetFanout
from failures import CoordinationTransient, error_code, is_condition_failed, is_transient
from logger import log
from reconcile import Reconciler, _reconciler
from signals import DEAD
from stores import Registry

#: The reconciler's actions after which a row is still waiting and due again.
_STILL_WAITING = {"none", "parked", "alive", "superseded"}


class Sweeper:
    def __init__(self, reconciler: Reconciler, *, owner: Optional[str] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
                 consumers_table: str = env.ASSET_SUBSCRIPTIONS_TABLE,
                 lambda_client: Any = None, fanout_function: str = env.ASSET_FANOUT_FUNCTION):
        self.reconciler = reconciler
        self.lambda_client = lambda_client
        self.fanout_function = fanout_function
        self.tasks = reconciler.tasks
        self.owner = owner or uuid.uuid4().hex
        self.monotonic = monotonic
        self.fanout = AssetFanout(reconciler, subscriptions_table=subscriptions_table, consumers_table=consumers_table)
        self.consumers = reconciler.resource.Table(consumers_table)
        self.facts = reconciler.resource.Table(reconciler.asset_events_table)

    def run(self) -> Dict[str, Any]:
        now = self.reconciler.clock()
        if not self._take_lease(now):
            log.info("sweeper", "another run holds the lease")
            return {"skipped": "lease held"}
        deadline = self.monotonic() + sweep.BUDGET.total_seconds()
        try:
            result = self.due_rows(deadline=deadline)
            result.update(self.paused_rows(deadline=deadline))
            result.update(self.push_retries(deadline=deadline))
            result.update(self.catch_up(deadline=deadline))
            result.update(self.adopt(deadline=deadline))
            result.update(self.self_check())
        finally:
            self._release_lease()
        log.info("sweeper", "done", **result)
        return result

    # ── lease ───────────────────────────────────────────────────────────

    def _take_lease(self, now: datetime) -> bool:
        try:
            self.tasks.table.put_item(
                Item={"execution_name": sweep.LEASE_KEY, "owner": self.owner,
                      "expires_at": format_utc(now + sweep.LEASE)},
                ConditionExpression="attribute_not_exists(execution_name) OR expires_at < :now",
                ExpressionAttributeValues={":now": format_utc(now)},
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False
            raise
        return True

    def _release_lease(self) -> None:
        try:
            self.tasks.table.delete_item(Key={"execution_name": sweep.LEASE_KEY},
                                         ConditionExpression="#o = :me",
                                         ExpressionAttributeNames={"#o": "owner"},
                                         ExpressionAttributeValues={":me": self.owner})
        except (ClientError, BotoCoreError) as exc:
            # The lease expires on its own; the next run takes it then.
            log.warn("sweeper", "could not release the lease", error=str(exc))

    # ── due rows ────────────────────────────────────────────────────────

    def due_rows(self, *, deadline: float) -> Dict[str, Any]:
        """Re-check every waiting row that is due, earliest first, until `deadline` (monotonic)."""
        now = self.reconciler.clock()
        counts: Dict[str, Any] = {"checked": 0, "woken": 0, "missed": 0, "transient": 0, "budget_reached": False}
        for row in self._due(now):
            if self.monotonic() >= deadline:
                counts["budget_reached"] = True     # the rest stay due: the next run starts with them
                break
            counts["checked"] += 1
            name = str(row["execution_name"])
            try:
                result = self.reconciler.reconcile(name, source=sweep.SOURCE)
            except CoordinationTransient as exc:
                counts["transient"] += 1
                log.error("sweeper", "reconcile failed transiently; due again next run", execution_name=name,
                          error=str(exc))
                continue
            if result["action"] == "signalled":
                counts["woken"] += 1
                if self._missed(row, result):
                    counts["missed"] += 1
            elif result["verdict"] in (dc.WAIT, dc.PAUSED) and result["action"] in _STILL_WAITING:
                self._due_again(row, now)
        return counts

    def _due(self, now: datetime, kind: str = att.WAITING) -> Iterator[Dict[str, Any]]:
        """Due rows of one attention kind, every shard merged into one stream ordered by due time."""
        shards = [self._shard(partition, now) for partition in att.partitions(kind)]
        return heapq.merge(*shards, key=lambda row: str(row[att.DUE]))

    def _shard(self, partition: str, now: datetime) -> Iterator[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {
            "IndexName": att.INDEX,
            "KeyConditionExpression": Key(att.KEY).eq(partition) & Key(att.DUE).lte(format_utc(now)),
        }
        while True:
            response = self.tasks.table.query(**kwargs)
            yield from response.get("Items", [])
            if "LastEvaluatedKey" not in response:
                return
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _due_again(self, row: Dict[str, Any], now: datetime) -> None:
        attempts = int(row.get("attempts", 0))
        name = str(row["execution_name"])
        try:
            self.tasks.mark_due(name, str(row.get("started_at")), att.next_due(now, attempts), attempts + 1)
        except (ClientError, BotoCoreError) as exc:
            # The row stays due; the next run checks it again.
            log.error("sweeper", "could not set the next check", execution_name=name, error=str(exc))

    def _missed(self, row: Dict[str, Any], result: Dict[str, Any]) -> bool:
        """Write a `sweeper` warning when the woken task could run before its due time."""
        enabled_at = parse_utc(result["enabled_at"]) if result.get("enabled_at") else None
        due = parse_utc(str(row[att.DUE]))
        if enabled_at is None or not sweep.primary_path_missed(enabled_at, due):
            return False
        full = self.tasks.get(str(row["execution_name"])) or row      # the index does not project `date`
        self._sweeper_warning(full, f"The sweeper woke this task at its check due {format_utc(due)}, but it could "
                                    f"run from {format_utc(enabled_at)}: the wake-up hint was lost on the way.")
        return True

    def _sweeper_warning(self, row: Dict[str, Any], error: str) -> None:
        self._put_warning(warning_item(
            kind="sweeper", execution_name=str(row["execution_name"]),
            task_name=str(row.get("task_name", "")), pipeline_name=str(row.get("pipeline_name", "")),
            pipeline_execution=str(row.get("pipeline_execution", "")), task_date=str(row.get("date", "")),
            error=error, now=self.reconciler.clock(),
        ))

    def _put_warning(self, item: Dict[str, Any]) -> None:
        try:
            self.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "could not write the warning row", warning=item["execution_name"], error=str(exc))

    # ── paused rows ─────────────────────────────────────────────────────

    def paused_rows(self, *, deadline: float) -> Dict[str, Any]:
        """Release the tasks held in their pause wait whose run is no longer paused (ADR #130 §8 step 2).

        Resume releases them itself. It misses a pause token written while it
        ran (it reads the run's rows through an index) and one whose release
        failed; without this step such a task waits for its 12-hour pause
        heartbeat. A task of a run that is still paused is due again later.
        """
        now = self.reconciler.clock()
        counts: Dict[str, Any] = {"pause_checked": 0, "pause_released": 0}
        paused: Dict[str, bool] = {}
        for row in self._due(now, att.PAUSED):
            if self.monotonic() >= deadline:
                break                               # the rest stay due: the next run starts with them
            counts["pause_checked"] += 1
            try:
                if self._release_if_resumed(row, now, paused):
                    counts["pause_released"] += 1
            except (ClientError, BotoCoreError, CoordinationTransient) as exc:
                # The row stays due; the next run checks it again.
                log.error("sweeper", "could not check a paused task", execution_name=str(row["execution_name"]),
                          error=str(exc))
        return counts

    def _release_if_resumed(self, listed: Dict[str, Any], now: datetime, paused: Dict[str, bool]) -> bool:
        name, partition = str(listed["execution_name"]), str(listed[att.KEY])
        # The index only finds candidates; the row itself is read from the table (ADR #130 §3).
        row = self.tasks.get(name)
        token = str(row.get("pause_token", "")) if row else ""
        if row is None or not token or row.get("status") != WAITING_PAUSED:
            self.tasks.drop_pause_check(name, partition)       # it moved on; run_task normally drops the entry
            return False
        run = str(row.get("pipeline_execution", ""))
        if run not in paused:
            paused[run] = self.tasks.paused(run)
        if paused[run]:
            attempts = int(row.get("attempts", 0))
            self.tasks.mark_pause_due(name, token, att.next_due(now, attempts), attempts + 1)
            return False
        if self.reconciler.signals.succeed(token, {"resumed": True}) == DEAD:
            self.tasks.drop_pause_check(name, partition, token)  # nothing can release it any more
            return False
        self._sweeper_warning(row, "The run is no longer paused, but this task was still held in its pause wait: "
                                   "the resume did not reach it. The sweeper released it.")
        return True


    # ── push retries ────────────────────────────────────────────────────

    def push_retries(self, *, deadline: float) -> Dict[str, Any]:
        """Start again the push runs whose start failed transiently (ADR #130 §7, §8)."""
        now = self.reconciler.clock()
        counts = {"push_retried": 0, "push_started": 0, "push_given_up": 0}
        for listed in self._due(now, att.RETRY):
            if self.monotonic() >= deadline:
                break
            # The index only finds candidates; the row itself is read from the table (ADR #130 §3).
            row = self.tasks.get(str(listed["execution_name"]))
            if row is None:
                continue                # done meanwhile
            counts["push_retried"] += 1
            consumer, name = str(row.get("consumer", "")), str(row["run_name"])
            if now - parse_utc(str(row["first_failed_at"])) >= sweep.PUSH_RETRY_WINDOW:
                self._push_warning(row, f"push consumer {consumer} could not be started for 24 hours, "
                                        f"so the sweeper gave up; last error: {row.get('error', '')}")
                self._drop(row)
                counts["push_given_up"] += 1
                continue
            try:
                self.reconciler.signals.client.start_execution(
                    stateMachineArn=str(row["sfn_arn"]), name=name, input=str(row["run_input"]))
            except (ClientError, BotoCoreError) as exc:
                if error_code(exc) != "ExecutionAlreadyExists":
                    if is_transient(exc):
                        self._retry_again(row, now, f"{type(exc).__name__}: {exc}")
                    else:
                        self._push_warning(row, f"push consumer {consumer} could not be started: "
                                                f"{type(exc).__name__}: {exc}")
                        self._drop(row)
                    continue
            counts["push_started"] += 1
            self._drop(row)
        return counts

    def _retry_again(self, row: Dict[str, Any], now: datetime, error: str) -> None:
        attempts = int(row.get("attempts", 0)) + 1
        try:
            self.tasks.table.update_item(
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
            self.tasks.table.delete_item(Key={"execution_name": row["execution_name"]})
        except (ClientError, BotoCoreError) as exc:
            # A leftover row is retried again: its start is idempotent (deterministic name).
            log.error("sweeper", "could not delete the push retry row", run_name=row["run_name"], error=str(exc))

    def _push_warning(self, row: Dict[str, Any], error: str) -> None:
        self._put_warning(warning_item(kind="push", execution_name=f"push:{row['run_name']}", task_name="",
                                       pipeline_name=str(row.get("consumer", "")), pipeline_execution="",
                                       task_date="", error=error, now=self.reconciler.clock()))

    # ── catch-up ────────────────────────────────────────────────────────

    def catch_up(self, *, deadline: float) -> Dict[str, Any]:
        """Hourly: replay AssetFanout's push branch for the facts of the last 48 hours (ADR #130 §8).

        Runs start under deterministic names, so a run that already started
        is only an ExecutionAlreadyExists. Only facts the new writers wrote
        are replayed: the old code's own push path handled the others. A run
        that reaches its deadline keeps a cursor, and the next run goes on.
        """
        now = self.reconciler.clock()
        health = self.tasks.get(sweep.HEALTH_KEY) or {}
        cursor = str(health.get("catchup_cursor", ""))
        last = parse_utc(str(health["catchup_at"])) if health.get("catchup_at") else None
        if not cursor and not sweep.catch_up_due(now, last):
            return {"catch_up": "not due"}
        since = format_utc(now - sweep.CATCH_UP_WINDOW)
        replayed = 0
        for asset in sorted(self._subscribed_assets()):
            if asset <= cursor:
                continue
            if self.monotonic() >= deadline:
                self._save_catch_up(cursor=cursor)
                return {"catch_up": "partial", "facts_replayed": replayed}
            for fact in self._recent_facts(asset, since):
                if fact.get("fact_version") == FACT_VERSION:
                    self.fanout.push(fact)
                    replayed += 1
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

    def _save_catch_up(self, *, cursor: str = "", done_at: Optional[datetime] = None) -> None:
        if done_at is not None:
            update, values = "SET catchup_at = :at REMOVE catchup_cursor", {":at": format_utc(done_at)}
        else:
            update, values = "SET catchup_cursor = :c", {":c": cursor}
        try:
            self.tasks.table.update_item(Key={"execution_name": sweep.HEALTH_KEY}, UpdateExpression=update,
                                         ExpressionAttributeValues=values)
        except (ClientError, BotoCoreError) as exc:
            # The next run starts the catch-up over: replaying is idempotent.
            log.error("sweeper", "could not save the catch-up progress", error=str(exc))


    # ── adoption ────────────────────────────────────────────────────────

    def adopt(self, *, deadline: float) -> Dict[str, Any]:
        """Bring rows an older platform version wrote into the new model (ADR #130 §8 step 6).

        Two resumable scans, kept in `_engine_health` with the adoption
        generation they serve: waiting task rows get an attention entry, due
        now, so the sweeper reconciles them from their facts; facts get their
        partition fields (a MODIFY, which the INSERT-only stream ignores). The
        task scan repeats for an hour after it first finishes.
        """
        now = self.reconciler.clock()
        health = self.tasks.get(sweep.HEALTH_KEY) or {}
        if int(health.get("adoption_generation", 0)) != sweep.ADOPTION_GENERATION:
            health = {"adopt_tasks_cursor": "", "adopt_tasks_until": "", "adopt_facts_cursor": "", "adopt_facts_done": False}
        state = {k: health.get(k, "" if k != "adopt_facts_done" else False) for k in
                 ("adopt_tasks_cursor", "adopt_tasks_until", "adopt_facts_cursor", "adopt_facts_done")}
        counts = {"tasks_adopted": 0, "facts_adopted": 0}
        until = str(state["adopt_tasks_until"])
        if state["adopt_tasks_cursor"] or not until or now < parse_utc(until):
            cursor, finished = self._scan(self.tasks.table, self._task_filter(), str(state["adopt_tasks_cursor"]),
                                          self._adopt_task, counts, "tasks_adopted", deadline)
            state["adopt_tasks_cursor"] = cursor
            if finished and not until:
                state["adopt_tasks_until"] = format_utc(now + sweep.ADOPTION_REPEAT)
        if not state["adopt_facts_done"] and self.monotonic() < deadline:
            registry = Registry(self.reconciler.resource, self.reconciler.pipelines_table)
            cursor, finished = self._scan(self.facts, {"FilterExpression": "attribute_not_exists(partition_date)"},
                                          str(state["adopt_facts_cursor"]),
                                          lambda item: self._adopt_fact(item, registry), counts, "facts_adopted",
                                          deadline)
            state["adopt_facts_cursor"], state["adopt_facts_done"] = cursor, finished
        self._save_health({"adoption_generation": sweep.ADOPTION_GENERATION, **state})
        return counts

    @staticmethod
    def _task_filter() -> Dict[str, Any]:
        statuses = sorted(RECONCILABLE)
        return {
            "FilterExpression": f"#s IN ({', '.join(f':s{i}' for i in range(len(statuses)))}) "
                                "AND attribute_not_exists(#att)",
            "ExpressionAttributeNames": {"#s": "status", "#att": att.KEY},
            "ExpressionAttributeValues": {f":s{i}": s for i, s in enumerate(statuses)},
        }

    def _scan(self, table: Any, query: Dict[str, Any], cursor: str, adopt: Callable[[Dict[str, Any]], bool],
              counts: Dict[str, int], counter: str, deadline: float) -> tuple:
        """Scan from `cursor` until done or out of budget; returns (cursor, finished)."""
        kwargs = dict(query)
        if cursor:
            kwargs["ExclusiveStartKey"] = json.loads(cursor)
        while self.monotonic() < deadline:
            response = table.scan(**kwargs)
            for item in response.get("Items", []):
                counts[counter] += adopt(item)
            if "LastEvaluatedKey" not in response:
                return "", True
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return json.dumps(kwargs.get("ExclusiveStartKey", {}), default=str) if "ExclusiveStartKey" in kwargs else "", False

    def _adopt_task(self, item: Dict[str, Any]) -> bool:
        """Give a row the attention entry the new code would have written; due now.

        A row held in run_task's pause wait goes to ``paused#``, which the
        sweeper releases once its run is no longer paused; any other to
        ``waiting#``, which it reconciles (ADR #130 invariant 5).
        """
        name = str(item["execution_name"])
        if item.get("pause_token"):
            kind, values = att.PAUSED, {":paused": WAITING_PAUSED}
            condition = "#s = :paused AND attribute_exists(pause_token)"
        else:
            statuses = sorted(RECONCILABLE)
            kind, values = att.WAITING, {f":s{i}": s for i, s in enumerate(statuses)}
            condition = (f"#s IN ({', '.join(f':s{i}' for i in range(len(statuses)))}) "
                         "AND attribute_not_exists(pause_token)")
        try:
            self.tasks.table.update_item(
                Key={"execution_name": name},
                UpdateExpression="SET #att = :att, #due = :due",
                ConditionExpression=condition + " AND attribute_not_exists(#att)",
                ExpressionAttributeNames={"#s": "status", "#att": att.KEY, "#due": att.DUE},
                ExpressionAttributeValues={":att": att.attention(kind, name),
                                           ":due": format_utc(self.reconciler.clock()), **values},
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False                    # it moved on meanwhile
            raise
        return True

    def _adopt_fact(self, item: Dict[str, Any], registry: Registry) -> bool:
        key = str(item.get("execution_date") or "")
        try:
            declared = registry.declared_granularity(str(item.get("source_dag", "")), str(item["asset_name"]))
            fact = fact_from_item(item, declared_granularity=declared)
            start = partition_date(key)
        except ValueError as exc:
            # A row nobody could read is not adopted; asset conditions skip it the same way.
            log.error("sweeper", "an old fact cannot be adopted", asset_name=item.get("asset_name"),
                      event_time=item.get("event_time"), error=str(exc))
            return False
        try:
            self.facts.update_item(
                Key={"asset_name": item["asset_name"], "event_time": item["event_time"]},
                UpdateExpression="SET partition_key = :k, partition_date = :d, granularity = :g",
                ConditionExpression="attribute_not_exists(partition_date)",
                ExpressionAttributeValues={":k": key, ":d": start.isoformat(), ":g": fact.granularity},
            )
        except ClientError as exc:
            if is_condition_failed(exc):
                return False
            raise
        return True

    # ── self-check ──────────────────────────────────────────────────────

    def self_check(self) -> Dict[str, Any]:
        """Write what the console's health check reads into `_engine_health` (ADR #130 §9)."""
        now = self.reconciler.clock()
        report = {
            "checked_at": format_utc(now),
            "fanout_mapping": self._mapping_state(),
            "stream": self._stream_state(),
            "oldest_overdue": self._oldest_due(now),
            "push_retry_backlog": self._backlog(),
        }
        self._save_health(report)
        problems = sweep.engine_problems(report, now)
        if problems:
            log.error("sweeper", "the coordination engine is unhealthy", problems=problems)
        return {"healthy": not problems}

    def _mapping_state(self) -> str:
        client = self.lambda_client or env.lambda_()
        try:
            mappings = client.list_event_source_mappings(FunctionName=self.fanout_function)["EventSourceMappings"]
        except (ClientError, BotoCoreError) as exc:
            return f"unknown ({error_code(exc) or type(exc).__name__})"
        return str(mappings[0].get("State", "unknown")) if mappings else "missing"

    def _stream_state(self) -> str:
        try:
            table = self.reconciler.resource.meta.client.describe_table(
                TableName=self.reconciler.asset_events_table)["Table"]
        except (ClientError, BotoCoreError) as exc:
            return f"unknown ({error_code(exc) or type(exc).__name__})"
        return sweep.STREAM_ENABLED if table.get("StreamSpecification", {}).get("StreamEnabled") else "DISABLED"

    def _oldest_due(self, now: datetime) -> str:
        """The earliest due time among the waiting and paused rows still due after this run."""
        oldest = ""
        for partition in att.partitions(att.WAITING) + att.partitions(att.PAUSED):
            items = self.tasks.table.query(
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
                response = self.tasks.table.query(**kwargs)
                total += int(response.get("Count", 0))
                if "LastEvaluatedKey" not in response:
                    break
                kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return total

    def _save_health(self, fields: Dict[str, Any]) -> None:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {f":v{i}": v for i, v in enumerate(fields.values())}
        try:
            self.tasks.table.update_item(
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
    sweeper = _SWEEPER or Sweeper(_reconciler(), owner=owner)
    return sweeper.run()
