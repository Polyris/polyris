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
(Principle #41). The same run then retries the push starts that failed
transiently, and once an hour replays the push branch for the facts of the
last 48 hours, so a lost stream record cannot lose a push start either.
"""

import heapq
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from coordination_core import attention as att
from coordination_core import decide as dc
from coordination_core import sweep
from coordination_core.records import FACT_VERSION, warning_item
from coordination_core.times import format_utc, parse_utc
import env
from asset_fanout import AssetFanout
from failures import CoordinationTransient, error_code, is_condition_failed, is_transient
from logger import log
from reconcile import Reconciler, _reconciler

#: The reconciler's actions after which a row is still waiting and due again.
_STILL_WAITING = {"none", "parked", "alive", "superseded"}


class Sweeper:
    def __init__(self, reconciler: Reconciler, *, owner: Optional[str] = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
                 consumers_table: str = env.ASSET_SUBSCRIPTIONS_TABLE):
        self.reconciler = reconciler
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
            result.update(self.push_retries(deadline=deadline))
            result.update(self.catch_up(deadline=deadline))
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
        item = warning_item(
            kind="sweeper", execution_name=str(row["execution_name"]),
            task_name=str(full.get("task_name", "")), pipeline_name=str(full.get("pipeline_name", "")),
            pipeline_execution=str(full.get("pipeline_execution", "")), task_date=str(full.get("date", "")),
            error=(f"The sweeper woke this task at its check due {format_utc(due)}, but it could run from "
                   f"{format_utc(enabled_at)}: the wake-up hint was lost on the way."),
            now=self.reconciler.clock(),
        )
        try:
            self.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "could not write the warning row", warning=item["execution_name"], error=str(exc))
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
        item = warning_item(kind="push", execution_name=f"push:{row['run_name']}", task_name="",
                            pipeline_name=str(row.get("consumer", "")), pipeline_execution="", task_date="",
                            error=error, now=self.reconciler.clock())
        try:
            self.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "could not write the warning row", warning=item["execution_name"], error=str(exc))

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


_SWEEPER: Optional[Sweeper] = None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    owner = getattr(context, "aws_request_id", None) or uuid.uuid4().hex
    sweeper = _SWEEPER or Sweeper(_reconciler(), owner=owner)
    return sweeper.run()
