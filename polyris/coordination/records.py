"""Row shapes the coordination layer writes and reads (ADR #130 §1, §9).

Builders only — no I/O. Every writer (run_task's template aside) goes
through these, so the bell, the index and the evaluator see one shape.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from ..partitions import GRANULARITIES, detect_granularity
from . import attention as att
from . import retention
from .asset_conditions import Fact
from .partition import partition_date
from .statuses import FAILED, WARNING_KINDS
from .sweep import next_push_retry
from .times import format_utc, parse_utc

WARNING_TTL = timedelta(days=8)
WARNING_PREFIX = "_notify_warn_"

RETRY_PUSH_PREFIX = "_retry_push_"
#: The key prefix of a run's DAG snapshot row, which the generated pipeline state machine writes
#: (``generators._build_registration_chain``) and the console reads; it is an internal row.
DAG_SNAPSHOT_PREFIX = "dag_snapshot::"
#: A push retry row lives a week; the sweeper gives up on it after a day (ADR #130 §7).
RETRY_TTL = timedelta(days=7)

CASCADES = frozenset({"auto", "none", "all"})

#: The events of a task's timeline (``task-events``) that a pass writes after its decision (ADR #137 §1), named as
#: the console's ``EventType`` names them, by the transition that writes each (``transitions.py``).
DEPS_READY = "DEPS_READY"
DEPS_SKIPPED = "DEPS_SKIPPED"
DEPS_BLOCKED = "DEPS_BLOCKED"
DECISION_EVENTS = {"ready": DEPS_READY, "skip_by_rule": DEPS_SKIPPED, "block": DEPS_BLOCKED}
#: The sort-key suffix of a decision's event: before run_task's TASK_STARTED (``#10``), as 1.2's wrapper wrote it.
DECISION_EVENT_ORDER = "#05"
TASK_FINISHED = "TASK_FINISHED"
#: The dispatcher's moves that fail a task whose failure run_task never recorded: each ends the task's timeline
#: with TASK_FINISHED failed, as run_task's Emit_Task_Finished_Failed does when it asks for a decision.
#: fail_undecided follows run_task's own event.
FAILURE_EVENTS = frozenset({"fail_wait", "fail_start", "fail_dead"})
#: After run_task's TASK_STARTED (#10) in a timeline's order, as run_task's TASK_FINISHED is.
FINISHED_EVENT_ORDER = "#20"
#: The characters of a cause the timeline keeps, as 1.2 did; the row keeps more, the execution all of it.
ERROR_SUMMARY_MAX = 500
#: Every fact carries it, though 1.3 reads it nowhere: 1.2's readers skip a fact without it, and a rollback
#: of the upgrade runs them again (Principle #39). 1.4 stops writing it.
FACT_VERSION = 2

#: The GSI on ``asset-events`` that asset conditions read (template.yaml).
#: Its items carry exactly what :func:`fact_from_item` needs.
PARTITION_INDEX = "asset-partition-index"
PARTITION_INDEX_PROJECTION = ("event_time", "partition_key", "granularity", "cascade", "source_dag", "uri")


def warning_item(
    *,
    kind: str,
    execution_name: str,
    task_name: str,
    pipeline_name: str,
    pipeline_execution: str,
    task_date: str,
    error: str,
    now: datetime,
) -> Dict[str, Any]:
    """A warning row for the console bell.

    Indexed by write time through the attention index, so warnings about
    tasks of any partition date reach the bell (defect B-N1). It carries no
    ``date`` attribute on purpose: the date-partitioned failure query must
    not list it a second time. ``task_date`` keeps the partition for display.

    Context the caller does not know is left out, never written as ``""``:
    ``pipeline_name`` and ``pipeline_execution`` are GSI keys, and DynamoDB
    rejects an empty string there — the warning would be lost.
    """
    if kind not in WARNING_KINDS:
        raise ValueError(f"unknown warning kind {kind!r}; expected one of {sorted(WARNING_KINDS)}")
    context = {"task_name": task_name, "pipeline_name": pipeline_name,
               "pipeline_execution": pipeline_execution, "task_date": task_date}
    return {
        "execution_name": f"{WARNING_PREFIX}{kind}_{execution_name}",
        "kind": kind,
        **{k: v for k, v in context.items() if v},
        "status": FAILED,
        "error": error,
        "finished_at": format_utc(now),
        att.KEY: att.attention(att.WARN, execution_name),
        att.DUE: format_utc(now),
        "ttl": int((now + WARNING_TTL).timestamp()),
    }


def push_retry_item(
    *,
    run_name: str,
    consumer: str,
    sfn_arn: str,
    run_input: str,
    error: str,
    now: datetime,
) -> Dict[str, Any]:
    """A push start that failed transiently, for the sweeper to retry (ADR #130 §7).

    Keyed by the run's deterministic name, so a repeated failure finds the
    same row and a retried start can never run the consumer twice.
    """
    if not (run_name.strip() and consumer.strip() and sfn_arn.strip()):
        raise ValueError("a push retry needs the run name, the consumer and its state machine")
    return {
        "execution_name": f"{RETRY_PUSH_PREFIX}{run_name}",
        "kind": "push",
        "consumer": consumer,
        "sfn_arn": sfn_arn,
        "run_name": run_name,
        "run_input": run_input,
        "error": error,
        "attempts": 0,
        "first_failed_at": format_utc(now),
        att.KEY: att.attention(att.RETRY, run_name),
        att.DUE: format_utc(next_push_retry(now, 0)),
        "ttl": int((now + RETRY_TTL).timestamp()),
    }


def cascade_of(run_input: Mapping[str, Any]) -> str:
    """The cascade of the facts a run records, from the flags its backfill started it with (ADR #57).

    Start_Run keeps it in the run's input, from which the dispatcher passes it
    to run_task, which writes it on each fact; the console uses this for Mark
    Successful. A suppressed run wins over one that cascades to all.
    """
    if run_input.get("_suppress_asset_event") is True:
        return "none"
    if run_input.get("cascade_all") is True:
        return "all"
    return "auto"


def decision_event(*, transition: str, task_run_id: str, execution_name: str, task_name: str,
                   pipeline_execution: str, partition_key: str, backfill_id: str, now: datetime,
                   dependencies: Optional[List[str]] = None, reasons: Optional[List[str]] = None) -> Dict[str, Any]:
    """The timeline event of a pass's decision, with the fields 1.2's wrapper wrote: ``dependencies`` on a ready
    row, the ``reason`` of a skip or a block. ``task_run_id`` is the row's run_task name, so each generation has
    a timeline of its own (invariant 4). It lives as long as a timeline does (``retention.TIMELINE``)."""
    event_type = DECISION_EVENTS[transition]
    item: Dict[str, Any] = {
        "task_run_id": task_run_id, "event_time": format_utc(now) + DECISION_EVENT_ORDER, "event_type": event_type,
        "execution_name": execution_name, "parent_execution_id": pipeline_execution, "backfill_id": backfill_id,
        "partition_key": partition_key, "task_name": task_name, "ttl": retention.ttl(now, retention.TIMELINE)}
    if event_type == DEPS_READY:
        item["dependencies"] = json.dumps(list(dependencies or []))
    else:
        item["reason"] = "; ".join(reasons or []) or ("Upstream task failed" if event_type == DEPS_BLOCKED
                                                      else "Trigger rule condition did not occur")
    return item


MANUAL_DECISION = "MANUAL_DECISION"
#: After run_task's TASK_FINISHED (#20), as the console's decision events have always sorted.
MANUAL_DECISION_ORDER = "#30"


def manual_decision_event(*, task_run_id: str, execution_name: str, task_name: str, pipeline_execution: str,
                          decision: str, reason: str, operator: str, now: datetime) -> Dict[str, Any]:
    """An operator's action in the task's timeline (``actions.task_action``): the decision, its reason and who
    took it, under the row's run_task name, for the timeline's 90 days."""
    return {"task_run_id": task_run_id, "event_time": format_utc(now) + MANUAL_DECISION_ORDER,
            "event_type": MANUAL_DECISION, "execution_name": execution_name, "parent_execution_id": pipeline_execution,
            "task_name": task_name, "decision": decision, "reason": reason or f"Manual {decision} via UI",
            "operator": operator, "ttl": retention.ttl(now, retention.TIMELINE)}


def error_summary(cause: str) -> str:
    """A cause as a timeline event keeps it; run_task's Emit_Task_Finished_Failed cuts it the same way."""
    return cause if len(cause) <= ERROR_SUMMARY_MAX else cause[:ERROR_SUMMARY_MAX] + "..."


def failure_event(*, task_run_id: str, execution_name: str, task_name: str, pipeline_execution: str,
                  partition_key: str, backfill_id: str, now: datetime, cause: str) -> Dict[str, Any]:
    """The end of the timeline of a task the dispatcher failed (FAILURE_EVENTS): TASK_FINISHED failed with its
    cause, the fields run_task writes. Under the row's run_task name, as the decision events (invariant 4)."""
    return {"task_run_id": task_run_id, "event_time": format_utc(now) + FINISHED_EVENT_ORDER,
            "event_type": TASK_FINISHED, "execution_name": execution_name, "parent_execution_id": pipeline_execution,
            "backfill_id": backfill_id, "partition_key": partition_key, "task_name": task_name, "status": FAILED,
            "error_summary": error_summary(cause), "ttl": retention.ttl(now, retention.TIMELINE)}


def asset_fact_item(
    *,
    asset_name: str,
    partition_key: str,
    granularity: str,
    cascade: str,
    source_dag: str,
    source_task: str,
    uri: str,
    now: datetime,
    ttl_days: int,
) -> Dict[str, Any]:
    """An ``asset-events`` row as every ADR #130 writer stores it.

    ``execution_date`` keeps the raw partition key for the readers that
    already use it (Principle #39); ``partition_date`` is the normalised date
    the partition index sorts on.
    """
    if not asset_name.strip():
        raise ValueError("asset_name must not be empty")
    if granularity not in GRANULARITIES:
        raise ValueError(f"unknown granularity {granularity!r}; expected one of {list(GRANULARITIES)}")
    if cascade not in CASCADES:
        raise ValueError(f"unknown cascade {cascade!r}; expected one of {sorted(CASCADES)}")
    if ttl_days < 1:
        raise ValueError("ttl_days must be at least 1")
    return {
        "asset_name": asset_name,
        "event_time": format_utc(now),
        "execution_date": partition_key,
        "partition_key": partition_key,
        "partition_date": partition_date(partition_key).isoformat(),
        "granularity": granularity,
        "cascade": cascade,
        "source_dag": source_dag,
        "source_task": source_task,
        "uri": uri,
        "fact_version": FACT_VERSION,
        "ttl": int((now + timedelta(days=ttl_days)).timestamp()),
    }


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _iso_date(text: str) -> date:
    """``YYYY-MM-DD`` only: ``date.fromisoformat`` also takes ``20261113``, a form no index key matches."""
    if not _ISO_DATE.fullmatch(text):
        raise ValueError(f"partition_date {text!r} is not YYYY-MM-DD")
    return date.fromisoformat(text)


def fact_from_item(item: Mapping[str, Any]) -> Fact:
    """Read an ``asset-events`` row, old or new, as a :class:`Fact`.

    Rows written before ADR #130 that 1.2 could not adopt have no
    ``partition_date`` and no ``granularity``: their date comes from
    ``execution_date``, and their granularity from the format of the key.
    """
    key = str(item.get("partition_key") or item.get("execution_date") or "")
    raw_date = item.get("partition_date")
    pd = _iso_date(str(raw_date)) if raw_date else partition_date(key)
    granularity = str(item.get("granularity") or detect_granularity(key))
    if granularity not in GRANULARITIES:
        raise ValueError(
            f"asset fact {item.get('asset_name')!r} has granularity {granularity!r}; "
            f"expected one of {list(GRANULARITIES)}"
        )
    return Fact(
        asset_name=str(item["asset_name"]),
        partition_date=pd,
        event_time=parse_utc(str(item["event_time"])),
        granularity=granularity,
    )
