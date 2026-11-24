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
  removal, the stop done and the engine's stop mark, each with the condition of
  §3 and §4, and each renewing the run row's history TTL (§5);
- the sweeper's move of the run row's due time after its check (§3).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from boto3.dynamodb.types import TypeSerializer

from ..constants import ExecutionStatus, TaskSpecKey
from . import attention, retention, row_write, run_end, run_name, run_row, start_run, task_row, task_spec, transitions
from .dispatch import Judgment, RowView, RunEnd, RunView
from .times import format_utc

_serializer = TypeSerializer()


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
    fields = dict(judgment.fields)
    if judgment.transition == "ready":
        if run is None:
            raise ValueError(f"{row.key}: a row is made ready only within its run")
        fields[task_row.TASK_RUN_ID] = run_name.run_task_name(row.key, run.run_start, row.generation)
    update = row_write.task_update(key=row.key, read_status=row.status, read_generation=row.generation,
                                   writer=transitions.Writer.DISPATCHER, transition=judgment.transition, fields=fields,
                                   removed=judgment.removed, due=judgment.due, table=table, now=now)
    items: List[Dict[str, Any]] = [{"Update": update}]
    if run is not None and (judgment.transition or _pause_moves_due(row, judgment, run)):
        items.append(version_check(run, table=table))
    return items


def applied(item: Mapping[str, Any], row: RowView, judgment: Judgment, run: Optional[RunView], *,
            now: datetime) -> Dict[str, Any]:
    """The row's item as ``task_write``'s update leaves it, so the rest of the pass reads what is written: its
    status, generation, times and due time. A test compares it with the item DynamoDB holds after the write."""
    after = dict(item)
    status = judgment.status or row.status
    after[task_row.TTL] = retention.ttl(now, retention.HISTORY)
    if judgment.transition:
        after[task_row.STATUS] = status
        if judgment.transition == "ready":
            if run is None:
                raise ValueError(f"{row.key}: a row is made ready only within its run")
            after[task_row.TASK_RUN_ID] = run_name.run_task_name(row.key, run.run_start, row.generation)
        move = transitions.task_transition(judgment.transition)
        if move.raises_generation:
            after[task_row.GENERATION] = row.generation + 1
        if move.skip_origin is not None:
            after[task_row.SKIP_ORIGIN] = move.skip_origin
    fields, removed = row_write.with_reasons_time(judgment.fields, judgment.removed, now)
    after.update(fields)
    for name in removed:
        after.pop(name, None)
    entry = row_write.entry_after(status, judgment.due, now, row.key)
    if entry is None:
        after.pop(attention.KEY, None)
        after.pop(attention.DUE, None)
    else:
        after[attention.KEY], after[attention.DUE] = entry
    return after


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
    wait_for = task_spec.readiness_from_text(str(item[task_row.READINESS])).get(TaskSpecKey.WAIT_FOR.value)
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
    """The answer's mark: the order of the token answered, while that token is still the current one and not yet
    marked (§4). It is a fact about the token, so it holds whatever raised ``run_version`` since the pass's read,
    and raises it whatever it is (D120): a Restart that lands between the pass's read and its answer then runs as
    a Restart after the run's end does, instead of the engine stopping the run for an answer it never saw marked."""
    if run.token_order is None:
        raise ValueError(f"{run.key}: a run row without a token has no answer to mark")
    return _run_update(run, table=table, now=now,
                       expression=f"SET #answered = :order, #finished = :now, {run_row.VERSION_RAISE}",
                       condition="#order = :order AND (attribute_not_exists(#answered) OR #answered <> :order)",
                       names={**run_row.VERSION_NAMES, "#answered": run_row.ANSWERED, "#order": run_row.TOKEN_ORDER,
                              "#finished": run_row.FINISHED_AT},
                       values={**run_row.VERSION_RAISE_VALUES, ":order": run.token_order, ":now": format_utc(now)})


def summary(run: RunView, end: RunEnd, *, table: str, read_at: datetime) -> Dict[str, Any]:
    """The run's summary, unless a pass that began reading later has written one (§4); it keeps ``run_version``.

    The summary of a finished run is written on the ``run_version`` the pass read instead: no later pass comes
    to replace it, so a count a clock running behind would refuse must not stay stale. No row leaves a final
    status without an action or a request, and each of them raises ``run_version``. With it goes the run's end
    (``finished_at``): the answer's mark writes the first end only, once per token (D120), and a run that a
    Restart after its end finishes again ends anew (the review of TR-6d, P-9).
    """
    names = {"#summary": run_row.SUMMARY, "#summary_at": run_end.SUMMARY_AT}
    values: Dict[str, Any] = {":summary": dict(end.summary), ":read_at": format_utc(read_at)}
    expression = "SET #summary = :summary, #summary_at = :read_at"
    if end.finished:
        return _run_update(run, table=table, now=read_at, expression=f"{expression}, #finished = :read_at",
                           condition=run_row.VERSION_UNCHANGED,
                           names={**names, "#finished": run_row.FINISHED_AT, **run_row.VERSION_NAMES},
                           values={**values, ":rv": run.version})
    return _run_update(run, table=table, now=read_at, expression=expression,
                       condition=f"attribute_exists(#key) AND ({run_end.SUMMARY_CONDITION})",
                       names={**names, "#key": run_row.TABLE_KEY}, values=values)


def due_now(run: RunView, *, table: str, now: datetime, left: bool) -> Dict[str, Any]:
    """The run row due at once; ``left`` also sets ``left_at``, which wakes the next pass (§7). It keeps ``run_version``."""
    expression = "SET #att = :att, #due = :now" + (", #left = :now" if left else "")
    names = {"#att": attention.KEY, "#due": attention.DUE, "#key": run_row.TABLE_KEY,
             **({"#left": run_row.LEFT_AT} if left else {})}
    return _run_update(run, table=table, now=now, expression=expression, condition="attribute_exists(#key)",
                       names=names,
                       values={":att": attention.attention(attention.WAITING, run.key), ":now": format_utc(now)})


def close(run: RunView, *, table: str, now: datetime) -> Dict[str, Any]:
    """The run row's due time removed, on the ``run_version`` the pass read (§3): a new token, an action or a
    request that lands meanwhile keeps it."""
    return _run_update(run, table=table, now=now, expression="REMOVE #att, #due, #left",
                       condition=run_row.VERSION_UNCHANGED,
                       names={**run_row.VERSION_NAMES, "#att": attention.KEY, "#due": attention.DUE,
                              "#left": run_row.LEFT_AT},
                       values={":rv": run.version})


def stop_done(run: RunView, *, table: str, now: datetime) -> Dict[str, Any]:
    """The stop recorded as done, once every row is final and the answer is marked (§4)."""
    return _run_update(run, table=table, now=now, expression=f"SET #done = :now, {run_row.VERSION_NEXT}",
                       condition=run_row.VERSION_UNCHANGED, names={**run_row.VERSION_NAMES, "#done": run_row.STOP_DONE},
                       values={**run_row.version_values(run.version), ":now": format_utc(now)})


def engine_stop(run: RunView, *, table: str, cause: str, now: datetime) -> Dict[str, Any]:
    """The engine's stop mark, with the cause (§4): only while no stop mark is set, which keeps who asked for a
    Stop of the run and why, and on the ``run_version`` the pass read, which it raises."""
    mark = run_row.stop_mark(by=run_row.ENGINE, reason=cause, at=format_utc(now))
    return _run_update(run, table=table, now=now, expression=f"SET #stop = :mark, {run_row.VERSION_NEXT}",
                       condition=f"{run_row.VERSION_UNCHANGED} AND attribute_not_exists(#stop)",
                       names={**run_row.VERSION_NAMES, "#stop": run_row.STOP},
                       values={**run_row.version_values(run.version), ":mark": mark})


def checked(run_key: str, *, table: str, due_read: str, version_read: int, checks: int,
            now: datetime, failed: bool = False) -> Dict[str, Any]:
    """After the sweeper's check of a run: its due time moved on by the backoff (``attention.next_due``, at most
    30 minutes) and ``left_at`` cleared (§3). Conditional on the due time and the ``run_version`` the sweeper read:
    it never postpones a check that a request, a pass or an action set meanwhile, and after a write that raised
    ``run_version`` the run row stays due for the next sweep. It keeps ``run_version``. A check whose pass failed for
    good marks the run row (``check_failed``), and any other clears the mark (§7)."""
    mark = ", #failed = :now REMOVE #left" if failed else " REMOVE #left, #failed"
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run_key}},
            "UpdateExpression": f"SET #due = :next, #checks = :checks, #ttl = :ttl{mark}",
            "ConditionExpression": f"#due = :read_due AND {run_row.VERSION_UNCHANGED}",
            "ExpressionAttributeNames": {"#due": attention.DUE, "#checks": run_row.CHECKS, "#ttl": run_row.TTL,
                                         "#left": run_row.LEFT_AT, "#failed": run_row.CHECK_FAILED,
                                         **run_row.VERSION_NAMES},
            "ExpressionAttributeValues": _typed({
                ":next": format_utc(attention.next_due(now, checks)), ":checks": checks + 1,
                ":ttl": retention.ttl(now, retention.HISTORY), ":read_due": due_read, ":rv": version_read,
                **({":now": format_utc(now)} if failed else {})})}


def check_failed(run_key: str, *, table: str, due_read: str, version_read: int, now: datetime) -> Dict[str, Any]:
    """After the sweeper's check of a run whose run row is not due yet, when its pass failed for good: the mark
    alone (§7). Until the run row's due time the sweeper leaves the run, whatever rows of it are due: their checks
    would meet the same failure. Conditional as ``checked``."""
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run_key}},
            "UpdateExpression": "SET #failed = :now, #ttl = :ttl",
            "ConditionExpression": f"#due = :read_due AND {run_row.VERSION_UNCHANGED}",
            "ExpressionAttributeNames": {"#due": attention.DUE, "#failed": run_row.CHECK_FAILED, "#ttl": run_row.TTL,
                                         **run_row.VERSION_NAMES},
            "ExpressionAttributeValues": _typed({
                ":now": format_utc(now), ":ttl": retention.ttl(now, retention.HISTORY), ":read_due": due_read,
                ":rv": version_read})}


def _run_update(run: RunView, *, table: str, now: datetime, expression: str, condition: str,
                names: Mapping[str, str], values: Mapping[str, Any]) -> Dict[str, Any]:
    """An update of the run row, which also renews its history TTL (§5); each is conditional on the row."""
    sets, _, removes = expression.partition("REMOVE ")
    sets = sets.strip()
    renew = "#ttl = :ttl"
    expression = f"{sets}, {renew}" if sets else f"SET {renew}"
    if removes:
        expression += f" REMOVE {removes}"
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run.key}}, "UpdateExpression": expression,
            "ConditionExpression": condition, "ExpressionAttributeNames": {**names, "#ttl": run_row.TTL},
            "ExpressionAttributeValues": _typed({**values, ":ttl": retention.ttl(now, retention.HISTORY)})}


def _typed(values: Mapping[str, Any]) -> Dict[str, Any]:
    return {name: _serializer.serialize(value) for name, value in values.items()}
