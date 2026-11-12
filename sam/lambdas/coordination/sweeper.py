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
(Principle #41).
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
from coordination_core.records import warning_item
from coordination_core.times import format_utc, parse_utc
from failures import CoordinationTransient, is_condition_failed
from logger import log
from reconcile import Reconciler, _reconciler

#: The reconciler's actions after which a row is still waiting and due again.
_STILL_WAITING = {"none", "parked", "alive", "superseded"}


class Sweeper:
    def __init__(self, reconciler: Reconciler, *, owner: Optional[str] = None,
                 monotonic: Callable[[], float] = time.monotonic):
        self.reconciler = reconciler
        self.tasks = reconciler.tasks
        self.owner = owner or uuid.uuid4().hex
        self.monotonic = monotonic

    def run(self) -> Dict[str, Any]:
        now = self.reconciler.clock()
        if not self._take_lease(now):
            log.info("sweeper", "another run holds the lease")
            return {"skipped": "lease held"}
        try:
            result = self.due_rows(deadline=self.monotonic() + sweep.BUDGET.total_seconds())
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

    def _due(self, now: datetime) -> Iterator[Dict[str, Any]]:
        """Due waiting rows of every shard, merged into one stream ordered by due time."""
        shards = [self._shard(partition, now) for partition in att.partitions(att.WAITING)]
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
        item = warning_item(
            kind="sweeper", execution_name=str(row["execution_name"]),
            task_name=str(row.get("task_name", "")), pipeline_name=str(row.get("pipeline_name", "")),
            pipeline_execution=str(row.get("pipeline_execution", "")), task_date=str(row.get("task_date", "")),
            error=(f"The sweeper woke this task at its check due {format_utc(due)}, but it could run from "
                   f"{format_utc(enabled_at)}: the wake-up hint was lost on the way."),
            now=self.reconciler.clock(),
        )
        try:
            self.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            log.error("sweeper", "could not write the warning row", warning=item["execution_name"], error=str(exc))
        return True


_SWEEPER: Optional[Sweeper] = None


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    owner = getattr(context, "aws_request_id", None) or uuid.uuid4().hex
    sweeper = _SWEEPER or Sweeper(_reconciler(), owner=owner)
    return sweeper.run()
