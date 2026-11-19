"""The writes of a dispatcher's pass, with their conditions (ADR #137 §1, §3, §4, §6, §7).

``dispatch`` decides; these functions build what the dispatcher sends. They
build for the low-level DynamoDB client, whose transactions take typed values,
and touch no AWS:

- a decision that moves a row is one transaction: the row's update, conditional
  on the status and the generation the pass read, and a ``ConditionCheck`` on the
  run row's ``run_version`` as the pass read it (§1). A stop also raises the row's
  generation, which fences a ``run_task`` still running (§3, invariant 6);
- the update of a row that waits on needs no ``ConditionCheck``, unless a pause
  removes or postpones its due time (§5): a Resume that lands after the read then
  keeps it;
- every write of a task row renews its history TTL (§5) and sets its entry in
  ``attention-index`` from its status and due time (§6);
- ``deps_ready`` records the name of the row's ``run_task`` execution, the name it
  is started under (``run_name.run_task_name``, invariant 4);
- the run's answer and its mark, the summary, the run row's due time and its
  removal, and the stop done, each with the condition of §3 and §4.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from boto3.dynamodb.types import TypeSerializer

from ..constants import ExecutionStatus, TaskSpecKey
from . import attention, retention, run_end, run_name, run_row, start_run, task_row, task_spec, transitions
from .dispatch import Judgment, RowView, RunEnd, RunView
from .times import format_utc

_serializer = TypeSerializer()
#: A task row's write is conditional on what the pass read of it.
ROW_CONDITION = "#status = :read AND #g = :g"


def task_write(row: RowView, judgment: Judgment, run: Optional[RunView], *, table: str,
               now: datetime) -> List[Dict[str, Any]]:
    """The transact items of ``judgment``'s write: none when nothing changes, the row's ``Update`` alone, or
    with the run row's ``ConditionCheck`` (a decision, or a pause that removes or postpones the due time).

    ``run`` is None for the stop of a row whose run row is missing (invariant 11): there is no version to check.
    One item is sent as ``UpdateItem``, two as ``TransactWriteItems``.
    """
    if judgment.task_id != row.task_id:
        raise ValueError(f"a judgment of {judgment.task_id!r} cannot write the row of {row.task_id!r}")
    if not judgment.transition and not judgment.fields and not judgment.removed and judgment.due == row.due:
        return []
    status = judgment.status or row.status
    names = {"#status": task_row.STATUS, "#g": task_row.GENERATION, "#ttl": task_row.TTL,
             "#att": attention.KEY, "#due": attention.DUE}
    values: Dict[str, Any] = {":read": row.status, ":g": row.generation,
                              ":ttl": retention.ttl(now, retention.HISTORY)}
    sets = ["#ttl = :ttl"]
    removes: List[str] = []
    fields = dict(judgment.fields)
    if judgment.transition:
        sets.append("#status = :status")
        values[":status"] = status
        if judgment.transition == "ready":
            if run is None:
                raise ValueError(f"{row.key}: a row is made ready only within its run")
            fields[task_row.TASK_RUN_ID] = run_name.run_task_name(row.key, run.run_start, row.generation)
        if judgment.transition == "stop":
            sets.append("#g = :g_next")
            values[":g_next"] = row.generation + 1
    for i, (name, value) in enumerate(sorted(fields.items())):
        names[f"#f{i}"] = name
        values[f":f{i}"] = value
        sets.append(f"#f{i} = :f{i}")
    for j, name in enumerate(judgment.removed):
        names[f"#r{j}"] = name
        removes.append(f"#r{j}")
    entry = _entry(status, judgment.due, now, row.key)
    if entry is None:
        removes += ["#att", "#due"]
    else:
        values[":att"], values[":due"] = entry
        sets += ["#att = :att", "#due = :due"]
    update = {"TableName": table, "Key": {task_row.TABLE_KEY: {"S": row.key}},
              "UpdateExpression": "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else ""),
              "ConditionExpression": ROW_CONDITION, "ExpressionAttributeNames": names,
              "ExpressionAttributeValues": _typed(values)}
    items: List[Dict[str, Any]] = [{"Update": update}]
    if run is not None and (judgment.transition or _pause_moves_due(row, judgment, run)):
        items.append(version_check(run, table=table))
    return items


def _entry(status: str, due: Optional[datetime], now: datetime, key: str) -> Optional[Tuple[str, str]]:
    """A row's entry in ``attention-index`` after a write: its kind by status, at its due time, or when it failed."""
    kind = transitions.attention_kind(status)
    if kind == attention.FAILED:
        return attention.attention(kind, key), format_utc(now)
    if kind is None or due is None:
        return None
    return attention.attention(kind, key), format_utc(due)


def _pause_moves_due(row: RowView, judgment: Judgment, run: RunView) -> bool:
    return run.paused and row.due is not None and (judgment.due is None or judgment.due > row.due)


def version_check(run: RunView, *, table: str) -> Dict[str, Any]:
    """The ``ConditionCheck`` of a decision: the run row's ``run_version`` is the one the pass read (§1)."""
    return {"ConditionCheck": {
        "TableName": table, "Key": {run_row.TABLE_KEY: {"S": run.key}},
        "ConditionExpression": run_row.VERSION_UNCHANGED, "ExpressionAttributeNames": dict(run_row.VERSION_NAMES),
        "ExpressionAttributeValues": _typed({":rv": run.version}),
    }}


def start_request(row: RowView, run: RunView, *, state_machine: str, input_text: str) -> Dict[str, str]:
    """``StartExecution`` of the row's ``run_task``, under the name its ``deps_ready`` write recorded."""
    return {"stateMachineArn": state_machine, "name": run_name.run_task_name(row.key, run.run_start, row.generation),
            "input": input_text}


def pointers(item: Mapping[str, Any]) -> List[Dict[str, str]]:
    """The keys of the ``asset:{name}`` pointers of a row read back, which go once it has left the wait (Rule 40)."""
    wait_for = task_spec.decision_from_text(str(item[task_row.DECISION])).get(TaskSpecKey.WAIT_FOR.value)
    return start_run.asset_pointer_keys(str(item[task_row.TABLE_KEY]), wait_for) if wait_for else []


def answer(run: RunView, end: RunEnd) -> Tuple[str, Dict[str, str]]:
    """The Step Functions call that answers the run's token: success, or ``FAILED_RUN_ERROR`` with the cause (§4)."""
    if not end.finished or run.token is None:
        raise ValueError(f"{run.key}: only a finished run with a token is answered")
    if end.outcome == ExecutionStatus.SUCCESS:
        return "send_task_success", {"taskToken": run.token,
                                     "output": json.dumps({"run": run.execution, "outcome": end.outcome})}
    return "send_task_failure", {"taskToken": run.token, "error": run_row.FAILED_RUN_ERROR,
                                 "cause": end.cause or str(end.outcome)}


def answered(run: RunView, *, table: str, now: datetime) -> Dict[str, Any]:
    """The answer's mark: the order of the token answered, while that token is still the current one (§4)."""
    if run.token_order is None:
        raise ValueError(f"{run.key}: a run row without a token has no answer to mark")
    return _run_update(run, table=table, expression=f"SET #answered = :order, #finished = :now, {run_row.VERSION_NEXT}",
                       condition=f"{run_row.VERSION_UNCHANGED} AND #order = :order",
                       names={**run_row.VERSION_NAMES, "#answered": run_row.ANSWERED, "#order": run_row.TOKEN_ORDER,
                              "#finished": run_row.FINISHED_AT},
                       values={**run_row.version_values(run.version), ":order": run.token_order,
                               ":now": format_utc(now)})


def summary(run: RunView, end: RunEnd, *, table: str, read_at: datetime) -> Dict[str, Any]:
    """The run's summary, unless a pass that began reading later has written one (§4); it keeps ``run_version``.

    The summary of a finished run is written on the ``run_version`` the pass read instead: no later pass comes
    to replace it, so a count a clock running behind would refuse must not stay stale. No row leaves a final
    status without an action or a request, and each of them raises ``run_version``.
    """
    names = {"#summary": run_row.SUMMARY, "#summary_at": run_end.SUMMARY_AT}
    values: Dict[str, Any] = {":summary": dict(end.summary), ":read_at": format_utc(read_at)}
    if end.finished:
        return _run_update(run, table=table, expression="SET #summary = :summary, #summary_at = :read_at",
                           condition=run_row.VERSION_UNCHANGED, names={**names, **run_row.VERSION_NAMES},
                           values={**values, ":rv": run.version})
    return _run_update(run, table=table, expression="SET #summary = :summary, #summary_at = :read_at",
                       condition=run_end.SUMMARY_CONDITION, names=names, values=values)


def due_now(run: RunView, *, table: str, now: datetime, left: bool) -> Dict[str, Any]:
    """The run row due at once; ``left`` also sets ``left_at``, which wakes the next pass (§7). It keeps ``run_version``."""
    expression = "SET #att = :att, #due = :now" + (", #left = :now" if left else "")
    names = {"#att": attention.KEY, "#due": attention.DUE, "#key": run_row.TABLE_KEY,
             **({"#left": run_row.LEFT_AT} if left else {})}
    return _run_update(run, table=table, expression=expression, condition="attribute_exists(#key)", names=names,
                       values={":att": attention.attention(attention.WAITING, run.key), ":now": format_utc(now)})


def close(run: RunView, *, table: str) -> Dict[str, Any]:
    """The run row's due time removed, on the ``run_version`` the pass read (§3): a new token, an action or a
    request that lands meanwhile keeps it."""
    return _run_update(run, table=table, expression="REMOVE #att, #due, #left", condition=run_row.VERSION_UNCHANGED,
                       names={**run_row.VERSION_NAMES, "#att": attention.KEY, "#due": attention.DUE,
                              "#left": run_row.LEFT_AT},
                       values={":rv": run.version})


def stop_done(run: RunView, *, table: str, now: datetime) -> Dict[str, Any]:
    """The stop recorded as done, once every row is final and the answer is marked (§4)."""
    return _run_update(run, table=table, expression=f"SET #done = :now, {run_row.VERSION_NEXT}",
                       condition=run_row.VERSION_UNCHANGED, names={**run_row.VERSION_NAMES, "#done": run_row.STOP_DONE},
                       values={**run_row.version_values(run.version), ":now": format_utc(now)})


def _run_update(run: RunView, *, table: str, expression: str, condition: str, names: Mapping[str, str],
                values: Mapping[str, Any]) -> Dict[str, Any]:
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run.key}}, "UpdateExpression": expression,
            "ConditionExpression": condition, "ExpressionAttributeNames": dict(names),
            "ExpressionAttributeValues": _typed(values)}


def _typed(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {name: _serializer.serialize(value) for name, value in values.items()}
