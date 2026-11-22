"""An operator's action on a task (ADR #137 §3): one pure builder for Skip, Fail, Mark Successful and Stop.

The console reads the task row, its run row and the date's ``output#`` item, and
sends the transaction built here from those reads. The action and everything
that must come with it are one transaction (invariants 6 and 12):

- the row's status, conditional on the status and the generation read, raising
  the generation (``row_write``);
- the run row's ``run_version`` raised whatever it is, and the run row due at once
  when it has no due time, so a pass that read the task before the action decides
  none of its dependants from that read, and after the run's end a lost stream
  record cannot leave its summary stale (§3);
- the output marker that dependants and the console read, unless real output is
  there: then the marker stays out and the caller warns (Principle #31). The
  marker's write is conditional on ``output#`` being as read, so a concurrent
  ``xcom.push`` cancels the transaction;
- for Mark Successful, a fact for each of the task's outlets.

A transaction refused because a row moved is built again from new reads, while
the action still applies. The timeline events and the stop of the row's
``run_task`` follow the transaction, best effort: the row holds the state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

from ..constants import TaskStatus
from ..partitions import detect_granularity
from . import attention, dispatch, records, retention, row_write, run_name, run_row, task_row, transitions
from .times import format_utc

#: The light actions on a task, named as their transitions (transitions.py).
TASK_ACTIONS = ("skip", "mark_success", "fail", "stop")
#: The fields of the output marker, in ``result`` and on the item; xcom.py reads the first (a parity test).
MARKER_RESOLVED = "_manually_resolved"
MARKER_RESOLUTION = "_resolution"
MARKER_REASON = "_reason"
MARKER_OPERATOR = "_operator"
MARKER_RUN = "_pipeline_execution"
#: The rows a running run_task holds: Stop stops that execution too.
_LIVE = frozenset({TaskStatus.DEPS_READY.value, TaskStatus.RUNNING.value})


class ActionRefused(ValueError):
    """The task's status allows no such action now, or its run is from before 1.3 (the console answers 409)."""


@dataclass(frozen=True)
class ActionWrite:
    """What an action sends: its transaction, then the events and the stop that follow it."""
    items: List[Dict[str, Any]]
    #: Real output is in ``output#``: the marker stays out, and the caller writes a ``manual`` warning.
    marker_blocked: bool = False
    #: Stop of a row whose run_task runs: the name of that execution, for StopExecution.
    run_task_to_stop: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)


def task_action(*, action: str, task: Mapping[str, Any], run: Mapping[str, Any], output: Optional[Mapping[str, Any]],
                outlets: Sequence[Mapping[str, Any]], cascade: str, operator: str, reason: str, now: datetime,
                tokens_table: str, asset_events_table: str, fact_ttl_days: int) -> ActionWrite:
    """The transaction of ``action`` on the task row ``task`` of the run row ``run``, from the caller's reads.

    ``output`` is the date's ``output#`` item as read (None when there is none); ``outlets`` are the task's spec's,
    which Mark Successful records; ``cascade`` is the run's (records.cascade_of).
    """
    if action not in TASK_ACTIONS:
        raise ValueError(f"no such action on a task: {action!r}")
    try:
        row = dispatch.row_view(task)
    except ValueError as exc:
        raise ActionRefused(f"{task.get(task_row.TABLE_KEY)}: a task of a run from before 1.3 takes no action") from exc
    view = dispatch.run_view(run)
    move = transitions.task_transition(action)
    if row.status not in move.sources:
        raise ActionRefused(f"{row.key}: a task {row.status} takes no {action}")
    stamp = format_utc(now)
    fields: Dict[str, Any] = {task_row.FINISHED_AT: stamp, task_row.STATUS_SINCE: stamp}
    if action == "fail":
        fields[task_row.ERROR] = reason
    items: List[Dict[str, Any]] = [
        {"Update": row_write.task_update(key=row.key, read_status=row.status, read_generation=row.generation,
                                         writer=transitions.Writer.CONSOLE, transition=action, fields=fields,
                                         removed=(), due=None, table=tokens_table, now=now)},
        {"Update": run_raise(view.key, table=tokens_table, now=now)},
    ]
    pipeline, task_name, date = str(task.get("pipeline_name", "")), row.task_id, view.date
    marker = output_marker(pipeline=pipeline, task_name=task_name, date=date, output=output, action=action,
                           reason=reason, operator=operator, run=view.execution, table=tokens_table, now=now)
    if marker is not None:
        items.append({"Update": marker})
    if action == "mark_success":
        items += [{"Put": fact} for fact in _facts(outlets, pipeline=pipeline, task_name=task_name, date=date,
                                                   cascade=cascade, now=now, ttl_days=fact_ttl_days,
                                                   table=asset_events_table)]
    run_task_name = run_name.run_task_name(row.key, view.run_start, row.generation)
    events = [records.manual_decision_event(task_run_id=run_task_name, execution_name=row.key, task_name=task_name,
                                            pipeline_execution=view.execution, decision=action, reason=reason,
                                            operator=operator, now=now)]
    error = task.get(task_row.ERROR)
    if error and action != "fail":
        # The failure an operator resolved stays in the timeline after a Skip or a Mark Successful (1.2 did so).
        events.insert(0, records.failure_event(task_run_id=run_task_name, execution_name=row.key, task_name=task_name,
                                               pipeline_execution=view.execution, partition_key=date,
                                               backfill_id=str(task.get(task_row.BACKFILL_ID) or ""), now=now,
                                               cause=str(error)))
    return ActionWrite(items=items, marker_blocked=marker is None and _real_output(output),
                       run_task_to_stop=run_task_name if action == "stop" and row.status in _LIVE else None,
                       events=events)


def run_raise(run_key: str, *, table: str, now: datetime) -> Dict[str, Any]:
    """An action's write of its run row: ``run_version`` raised whatever it is, and the run row due at once when it
    has no due time (§3); the history TTL renewed, as by every write of the run row (§5)."""
    names = {**run_row.VERSION_NAMES, "#att": attention.KEY, "#due": attention.DUE, "#ttl": run_row.TTL,
             "#key": run_row.TABLE_KEY}
    values = {**run_row.VERSION_RAISE_VALUES, ":att": attention.attention(attention.WAITING, run_key),
              ":now": format_utc(now), ":ttl": retention.ttl(now, retention.HISTORY)}
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run_key}},
            "UpdateExpression": f"SET {run_row.VERSION_RAISE}, #att = if_not_exists(#att, :att), "
                                "#due = if_not_exists(#due, :now), #ttl = :ttl",
            "ConditionExpression": "attribute_exists(#key)",
            "ExpressionAttributeNames": names, "ExpressionAttributeValues": row_write.typed(values)}


def output_marker(*, pipeline: str, task_name: str, date: str, output: Optional[Mapping[str, Any]], action: str,
                  reason: str, operator: str, run: str, table: str, now: datetime) -> Optional[Dict[str, Any]]:
    """The marker's write on ``output#``, or None when real output is there (Principle #31): it replaces nothing
    but an earlier marker, conditional on the item being as read, and lives a year from this write (§5)."""
    if _real_output(output):
        return None
    marker = {MARKER_RESOLVED: True, MARKER_RESOLUTION: action, MARKER_REASON: reason, MARKER_OPERATOR: operator,
              MARKER_RUN: run}
    prior = (output or {}).get("result")
    names = {"#r": "result", "#ttl": "ttl", "#m": MARKER_RESOLVED, "#res": MARKER_RESOLUTION, "#why": MARKER_REASON,
             "#who": MARKER_OPERATOR, "#run": MARKER_RUN}
    values: Dict[str, Any] = {":r": json.dumps(marker), ":m": True, ":res": action, ":why": reason, ":who": operator,
                              ":run": run, ":tn": task_name, ":now": format_utc(now),
                              ":ttl": retention.ttl(now, retention.OUTPUT)}
    condition = "attribute_not_exists(#r)"
    if prior is not None:
        condition = "#r = :prior"
        values[":prior"] = prior
    return {"TableName": table, "Key": {task_row.TABLE_KEY: {"S": f"output#{pipeline}#{task_name}#{date}"}},
            "UpdateExpression": "SET #r = :r, #m = :m, #res = :res, #why = :why, #who = :who, #run = :run, "
                                "task_name = :tn, updated_at = :now, #ttl = :ttl",
            "ConditionExpression": condition, "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": row_write.typed(values)}


def _real_output(output: Optional[Mapping[str, Any]]) -> bool:
    """``output#`` holds a value a task produced, not an earlier action's marker."""
    prior = (output or {}).get("result")
    if prior is None:
        return False
    if not isinstance(prior, str):
        return True                              # a shape no writer gives: kept, as real output is
    try:
        parsed = json.loads(prior)
    except ValueError:
        return True                              # a 1.2 string result: real output
    return not (isinstance(parsed, dict) and parsed.get(MARKER_RESOLVED) is True)


def _facts(outlets: Sequence[Mapping[str, Any]], *, pipeline: str, task_name: str, date: str, cascade: str,
           now: datetime, ttl_days: int, table: str) -> List[Dict[str, Any]]:
    """Mark Successful's facts: one per outlet, as run_task's success records them, at a time no other fact of the
    asset holds (a collision cancels the transaction, which the caller builds again at a later time)."""
    facts = []
    for outlet in outlets:
        name = str(outlet.get("name") or "")
        if not name:
            continue
        item = records.asset_fact_item(asset_name=name, partition_key=date,
                                       granularity=str(outlet.get("granularity") or detect_granularity(date)),
                                       cascade=cascade, source_dag=pipeline, source_task=task_name,
                                       uri=str(outlet.get("uri") or ""), now=now, ttl_days=ttl_days)
        facts.append({"TableName": table, "Item": row_write.typed(item),
                      "ConditionExpression": "attribute_not_exists(event_time)"})
    return facts
