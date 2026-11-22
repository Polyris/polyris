"""An operator's actions (ADR #137 §3): one pure builder for Skip, Fail, Mark Successful, Stop and Restart of a task,
and Stop, Pause and Resume of a run.

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

A Restart puts the row back to ``waiting`` with the run's request in the same
transaction: the renewal of the history TTL, and for Restart with dependants
the reset of the dependants that a failure or a stop decided. The dispatcher
carries the requests out in passes through the builders below (``renewals``,
``reset_set``, ``reset``, ``clear_request``), since they touch every row of a
run and the console answers within API Gateway's 30 seconds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..constants import SkipOrigin, TaskStatus
from ..partitions import detect_granularity
from . import (attention, deadlines, dispatch, dispatch_writes, pauses, records, retention, row_write, run_name,
               run_row, start_run, task_row, transitions, trigger_rules)
from .times import format_utc, parse_utc

#: The light actions on a task, named as their transitions (transitions.py).
TASK_ACTIONS = ("skip", "mark_success", "fail", "stop")
#: The fields of the output marker, in ``result`` and on the item; xcom.py reads the first (a parity test).
MARKER_RESOLVED = "_manually_resolved"
MARKER_RESOLUTION = "_resolution"
MARKER_REASON = "_reason"
MARKER_OPERATOR = "_operator"
MARKER_RUN = "_pipeline_execution"
#: The rows a running run_task holds: an action stops that execution too.
_LIVE = frozenset({TaskStatus.DEPS_READY.value, TaskStatus.RUNNING.value})
#: An operator's reason is free text: the row's error, the marker and the timeline take this much of it.
REASON_MAX = 4000
#: What a row's wait wrote that a final status no longer has: the reasons it waited, its countdown, the decision it
#: waited for. An action that ends the row removes them, so no stale reason or deadline stays on it.
_WAIT_FIELDS = (task_row.REASONS, task_row.COUNTDOWN_END, task_row.DECISION_SINCE, task_row.DECISION_TIMEOUT)


class ActionRefused(ValueError):
    """The task's status allows no such action now, or its run is from before 1.3 (the console answers 409)."""


@dataclass(frozen=True)
class ActionWrite:
    """What an action sends: its transaction, then the events and the stop that follow it."""
    items: List[Dict[str, Any]]
    #: Real output is in ``output#``: the marker stays out, and the caller writes a ``manual`` warning.
    marker_blocked: bool = False
    #: An action on a row whose run_task may run (``deps_ready``, ``running``): the name of that execution, which
    #: the caller stops after the transaction, as cleanup (§3).
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
    row = _row_of(task, action)
    view = dispatch.run_view(run)
    reason = reason[:REASON_MAX]
    stamp = format_utc(now)
    fields: Dict[str, Any] = {task_row.FINISHED_AT: stamp, task_row.STATUS_SINCE: stamp}
    if action == "fail":
        fields[task_row.ERROR] = reason
    items: List[Dict[str, Any]] = [
        {"Update": row_write.task_update(key=row.key, read_status=row.status, read_generation=row.generation,
                                         writer=transitions.Writer.CONSOLE, transition=action, fields=fields,
                                         removed=[f for f in _WAIT_FIELDS if f in task], due=None, table=tokens_table,
                                         now=now)},
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
    timeline = _timeline_name(task, run_task_name)
    events = [records.manual_decision_event(task_run_id=timeline, execution_name=row.key, task_name=task_name,
                                            pipeline_execution=view.execution, decision=action, reason=reason,
                                            operator=operator, now=now)]
    error = task.get(task_row.ERROR)
    if error and action != "fail":
        # The failure an operator resolved stays in the timeline after a Skip or a Mark Successful (1.2 did so).
        events.insert(0, records.failure_event(task_run_id=timeline, execution_name=row.key, task_name=task_name,
                                               pipeline_execution=view.execution, partition_key=date,
                                               backfill_id=str(task.get(task_row.BACKFILL_ID) or ""), now=now,
                                               cause=str(error)))
    return ActionWrite(items=items, marker_blocked=marker is None and _real_output(output),
                       run_task_to_stop=run_task_name if row.status in _LIVE else None,
                       events=events)


def _timeline_name(task: Mapping[str, Any], derived: str) -> str:
    """The name an action's events go under: the row's latest run_task, whose name the console reads the timeline
    by, or the generation's derived name before any run_task (a row that never became ready)."""
    return str(task.get(task_row.TASK_RUN_ID) or derived)


def run_raise(run_key: str, *, table: str, now: datetime) -> Dict[str, Any]:
    """An action's write of its run row: ``run_version`` raised whatever it is, and the run row due at once when it
    has no due time (§3); the history TTL renewed, as by every write of the run row (§5). Only an active run takes
    an action (the transition table): a run the engine closed before it became active stays closed."""
    names = {**run_row.VERSION_NAMES, "#att": attention.KEY, "#due": attention.DUE, "#ttl": run_row.TTL,
             "#key": run_row.TABLE_KEY, "#status": run_row.STATUS}
    values = {**run_row.VERSION_RAISE_VALUES, ":att": attention.attention(attention.WAITING, run_key),
              ":now": format_utc(now), ":ttl": retention.ttl(now, retention.HISTORY), ":active": run_row.ACTIVE}
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run_key}},
            "UpdateExpression": f"SET {run_row.VERSION_RAISE}, #att = if_not_exists(#att, :att), "
                                "#due = if_not_exists(#due, :now), #ttl = :ttl",
            "ConditionExpression": "attribute_exists(#key) AND #status = :active",
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


def _row_of(task: Mapping[str, Any], action: str) -> dispatch.RowView:
    """The row read back, refused for a run from before 1.3 or a status the table gives no such action from."""
    try:
        row = dispatch.row_view(task)
    except ValueError as exc:
        raise ActionRefused(f"{task.get(task_row.TABLE_KEY)}: a task of a run from before 1.3 takes no action") from exc
    if row.status not in transitions.task_transition(action).sources:
        raise ActionRefused(f"{row.key}: a task {row.status} takes no {action}")
    return row


# ── Restart, and Restart with dependants (§3) ─────────────────────────────


@dataclass(frozen=True)
class RestartWrite:
    """What a Restart sends: its transaction, then its timeline event. The pass that carries out its renewal
    writes the row's ``asset:`` pointers (``pointer_items``)."""
    items: List[Dict[str, Any]]
    events: List[Dict[str, Any]] = field(default_factory=list)


def restart(*, task: Mapping[str, Any], run: Mapping[str, Any], with_dependants: bool, operator: str, reason: str,
            now: datetime, tokens_table: str) -> RestartWrite:
    """The transaction of a Restart of the task row ``task`` of the run row ``run``, from the caller's reads.

    The row waits again as ``Start_Run`` wrote it, its limits counted anew; the run row raises ``run_version``,
    lives a year and 90 days from now, falls due at once and carries the request: the renewal of the history
    TTL, and with ``with_dependants`` the reset of the task's dependants (§3, §5).
    """
    row = _row_of(task, "restart")
    view = dispatch.run_view(run)
    refusal = restart_refusal(view, with_dependants=with_dependants, now=now)
    if refusal:
        raise ActionRefused(f"{row.key}: {refusal}")
    items = [{"Update": back_to_waiting(task, row, view, transition="restart", writer=transitions.Writer.CONSOLE,
                                        now=now, table=tokens_table)},
             {"Update": run_request(view, run, task_id=row.task_id if with_dependants else None, by=operator,
                                    now=now, table=tokens_table)}]
    event = records.manual_decision_event(
        task_run_id=_timeline_name(task, run_name.run_task_name(row.key, view.run_start, row.generation)),
        execution_name=row.key, task_name=row.task_id, pipeline_execution=view.execution, decision="restart",
        reason=reason[:REASON_MAX],
        operator=operator, now=now)
    return RestartWrite(items=items, events=[event])


def restart_refusal(run: dispatch.RunView, *, with_dependants: bool, now: datetime) -> Optional[str]:
    """Why the run takes no Restart now, or None (§3, §5)."""
    if not run.active:
        return "its run is not active yet"
    if run.stopping:
        return "a Stop of its run is in progress"
    if run.started_at is None or not retention.restart_allowed(run.started_at, now):
        return ("its run started more than a year ago, less a day: rows a rerun reads may be gone "
                "(run the date again instead)")
    if with_dependants and run_row.RESET_DEPENDANTS in run.requests:
        return "a Restart with dependants is in progress in its run"
    return None


def back_to_waiting(task: Mapping[str, Any], row: dispatch.RowView, run: dispatch.RunView, *, transition: str,
                    writer: str, now: datetime, table: str) -> Dict[str, Any]:
    """The row's ``Update`` of a Restart or a reset: ``waiting`` from now, without what its run wrote
    (``task_row.RUN_FIELDS``), with its due time counted anew (``deadlines.due_time``, invariant 9)."""
    fields, removed, due, _ = _waiting_again(task, row, run, now)
    return row_write.task_update(key=row.key, read_status=row.status, read_generation=row.generation, writer=writer,
                                 transition=transition, fields=fields, removed=removed, due=due, table=table, now=now)


def reset_applied(task: Mapping[str, Any], row: dispatch.RowView, run: dispatch.RunView, *,
                  now: datetime) -> Dict[str, Any]:
    """The row's item as a reset leaves it, so the rest of the pass reads what is written (a test compares it
    with the item DynamoDB holds after the reset)."""
    _, _, due, after = _waiting_again(task, row, run, now)
    after[task_row.GENERATION] = row.generation + 1
    after[task_row.TTL] = retention.ttl(now, retention.HISTORY)
    after.pop(attention.KEY, None)
    after.pop(attention.DUE, None)
    entry = row_write.entry_after(TaskStatus.WAITING.value, due, now, row.key)
    if entry is not None:
        after[attention.KEY], after[attention.DUE] = entry
    return after


def _waiting_again(task: Mapping[str, Any], row: dispatch.RowView, run: dispatch.RunView,
                   now: datetime) -> Tuple[Dict[str, Any], List[str], Optional[datetime], Dict[str, Any]]:
    """What a row back to ``waiting`` sets and removes, its due time, and the item it leaves."""
    stamp = format_utc(now)
    fields: Dict[str, Any] = {task_row.WAITING_SINCE: stamp, task_row.STATUS_SINCE: stamp}
    if not row.dependencies:
        fields[task_row.ENABLED_AT] = stamp           # allowed from the start of its wait, as Start_Run writes it
    after = {k: v for k, v in task.items() if k not in task_row.RUN_FIELDS}
    after.update(fields)
    after[task_row.STATUS] = TaskStatus.WAITING.value
    due = deadlines.due_time(task_row.row_times(after), run.pauses)
    removed = [name for name in task_row.RUN_FIELDS if name in task and name not in fields]
    return fields, removed, due, after


def run_request(run: dispatch.RunView, run_item: Mapping[str, Any], *, task_id: Optional[str], by: str, now: datetime,
                table: str) -> Dict[str, Any]:
    """A Restart's write of its run row (§3): ``run_version`` raised, the long TTL, due at once, and the request
    of the renewal, with the reset of ``task_id``'s dependants when it names one.

    It is conditional on the run being active with no stop in progress, and for a reset on none being in
    progress. The requests map is written whole when the run row has none yet, else by kind, as read."""
    stamp = format_utc(now)
    kinds: Dict[str, Dict[str, str]] = {run_row.RENEW_TTL: run_row.request(by=by, at=stamp)}
    if task_id is not None:
        kinds[run_row.RESET_DEPENDANTS] = run_row.request(by=by, at=stamp, task=task_id)
    names = {**run_row.VERSION_NAMES, "#status": run_row.STATUS, "#stop": run_row.STOP, "#done": run_row.STOP_DONE,
             "#req": run_row.REQUESTS, "#att": attention.KEY, "#due": attention.DUE, "#ttl": run_row.TTL}
    values: Dict[str, Any] = {**run_row.VERSION_RAISE_VALUES, ":active": run_row.ACTIVE, ":now": stamp,
                              ":att": attention.attention(attention.WAITING, run.key),
                              ":ttl": retention.ttl(now, retention.HISTORY)}
    condition = "#status = :active AND (attribute_not_exists(#stop) OR attribute_exists(#done))"
    if isinstance(run_item.get(run_row.REQUESTS), Mapping):
        sets = []
        for placeholder, kind in (("#renew", run_row.RENEW_TTL), ("#reset", run_row.RESET_DEPENDANTS)):
            if kind in kinds:
                names[placeholder] = kind
                values[f":{placeholder[1:]}"] = kinds[kind]
                sets.append(f"#req.{placeholder} = :{placeholder[1:]}")
        condition += " AND attribute_exists(#req)"
        if task_id is not None:
            condition += " AND attribute_not_exists(#req.#reset)"
    else:
        sets = ["#req = :requests"]
        values[":requests"] = kinds
        condition += " AND attribute_not_exists(#req)"
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run.key}},
            "UpdateExpression": f"SET {run_row.VERSION_RAISE}, #att = :att, #due = :now, #ttl = :ttl, "
                                + ", ".join(sets),
            "ConditionExpression": condition, "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": row_write.typed(values)}


def pointer_items(task: Mapping[str, Any], *, run_key: str, now: datetime) -> List[Dict[str, Any]]:
    """The row's ``asset:{name}`` pointers, as ``Start_Run`` writes them, for a row that waits again (Rule 40): the
    renewal writes those of each waiting row, a Restart's included, and a reset those of its row."""
    ttl = retention.ttl(now, retention.HISTORY)
    return [{**key, start_run.POINTER_RUN: run_key, start_run.POINTER_TTL: ttl}
            for key in dispatch_writes.pointers(task)]


def reset_set(rows: Mapping[str, dispatch.RowView], request: Mapping[str, Any]) -> List[str]:
    """The rows a pass resets for a Restart with dependants, upstreams first (§3): the descendants of the
    request's task among ``rows`` that a rule decided (``upstream_failed``, or skipped by a rule) and whose rule no
    longer gives that decision from their upstreams as they are now, the rows this set resets counting as waiting
    again. A dependant that another final failure still blocks stays, so no reset comes round again; an
    operator's or the run's skip stays (D121)."""
    dependants: Dict[str, List[str]] = {}
    for name, row in rows.items():
        for upstream in set(row.dependencies):
            dependants.setdefault(upstream, []).append(name)
    seen: set = set()
    stack = [str(request.get("task", ""))]
    while stack:                                       # iterative (polyris/CLAUDE.md, "Graph traversal")
        for dependant in dependants.get(stack.pop(), ()):
            if dependant not in seen:
                seen.add(dependant)
                stack.append(dependant)
    chosen: List[str] = []
    waiting_again: Dict[str, dispatch.RowView] = {}
    for name in dispatch.upstreams_first(seen, {n: rows[n].dependencies for n in seen}):
        row = rows[name]
        decided = _RULE_DECISIONS.get((row.status, row.skip_origin))
        if decided is None:
            continue
        upstreams = {d: waiting_again.get(d, rows.get(d)) for d in row.dependencies}
        if any(u is None for u in upstreams.values()) or dispatch.rule_verdict(row, upstreams) == decided:
            continue                                   # a missing upstream row fails it at its next decision
        chosen.append(name)
        waiting_again[name] = replace(row, status=TaskStatus.WAITING.value, skip_origin=None)
    return chosen


#: The statuses a rule decides, and the verdict that decided each (trigger_rules).
_RULE_DECISIONS = {(TaskStatus.UPSTREAM_FAILED.value, None): trigger_rules.UPSTREAM_FAILED,
                   (TaskStatus.SKIPPED.value, SkipOrigin.RULE): trigger_rules.SKIP}


def reset(task: Mapping[str, Any], row: dispatch.RowView, run: dispatch.RunView, *, now: datetime,
          table: str) -> List[Dict[str, Any]]:
    """One reset of a Restart with dependants (§3): the row back to ``waiting`` as a Restart writes it, and the
    run row's ``run_version`` raised from the value the pass read, so a decision read before it loses."""
    return [{"Update": back_to_waiting(task, row, run, transition="reset", writer=transitions.Writer.DISPATCHER,
                                       now=now, table=table)},
            {"Update": _version_next(run, table=table, now=now)}]


def clear_request(run: dispatch.RunView, kind: str, *, table: str, now: datetime) -> Dict[str, Any]:
    """The request ``kind`` cleared once carried out, on the ``run_version`` the pass read, which it raises."""
    return _version_next(run, table=table, now=now, remove=kind)


def _version_next(run: dispatch.RunView, *, table: str, now: datetime, remove: Optional[str] = None) -> Dict[str, Any]:
    names = {**run_row.VERSION_NAMES, "#ttl": run_row.TTL}
    expression = f"SET {run_row.VERSION_NEXT}, #ttl = :ttl"
    if remove is not None:
        names.update({"#req": run_row.REQUESTS, "#kind": remove})
        expression += " REMOVE #req.#kind"
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": run.key}}, "UpdateExpression": expression,
            "ConditionExpression": run_row.VERSION_UNCHANGED, "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": row_write.typed({**run_row.version_values(run.version),
                                                         ":ttl": retention.ttl(now, retention.HISTORY)})}


def renewal_keys(run: dispatch.RunView, names: Sequence[str]) -> List[str]:
    """The items besides the task rows that a Restart's renewal reads the TTL of: the run's input and each task's
    ``output#`` (§5)."""
    return [run_row.input_key(run.execution)] + [f"output#{run.pipeline}#{name}#{run.date}" for name in sorted(names)]


def renewals(run: dispatch.RunView, items: Mapping[str, Mapping[str, Any]], *, request: Mapping[str, Any],
             ttls: Mapping[str, int], now: datetime, table: str) -> List[Dict[str, Any]]:
    """The renewal of a Restart (§5), as single updates a pass sends one by one: the history TTL of each task row
    and of the run's input, and the year of each task's ``output#``, for each item that lives less than the request
    asks, less ``RENEWAL_SLACK``. ``ttls`` are the TTLs read of ``renewal_keys``; an item not read does not exist,
    and nothing is written for it.

    Each write is conditional on the item's existence, so none writes an item of the key alone where TTL deleted
    one after the read; a refused condition is no failure. Every task's ``output#``, not only the restarted task's
    upstreams: a reset row reads its other upstreams' outputs too, possibly long after the Restart."""
    at = parse_utc(str(request["at"]))
    history = retention.ttl(at - retention.RENEWAL_SLACK, retention.HISTORY)
    year = retention.ttl(at - retention.RENEWAL_SLACK, retention.OUTPUT)
    history_ttl, year_ttl = retention.ttl(now, retention.HISTORY), retention.ttl(now, retention.OUTPUT)
    writes = [_renewal(str(item[task_row.TABLE_KEY]), ttl=history_ttl, table=table)
              for _, item in sorted(items.items()) if int(item.get(task_row.TTL, 0)) < history]
    input_key, *outputs = renewal_keys(run, list(items))
    if input_key in ttls and ttls[input_key] < history:
        writes.append(_renewal(input_key, ttl=history_ttl, table=table))
    writes += [_renewal(key, ttl=year_ttl, table=table) for key in outputs if key in ttls and ttls[key] < year]
    return writes


def _renewal(key: str, *, ttl: int, table: str) -> Dict[str, Any]:
    return {"TableName": table, "Key": {task_row.TABLE_KEY: {"S": key}}, "UpdateExpression": "SET #ttl = :ttl",
            "ConditionExpression": "attribute_exists(#key)",
            "ExpressionAttributeNames": {"#key": task_row.TABLE_KEY, "#ttl": "ttl"},
            "ExpressionAttributeValues": row_write.typed({":ttl": ttl})}


# ── Stop of a run, Pause and Resume (§3, §4, §5) ──────────────────────────


def stop_run(run: Mapping[str, Any], *, by: str, reason: str, now: datetime, table: str) -> Optional[Dict[str, Any]]:
    """The console's Stop of a run (§4): the stop mark with who asked and why, ``run_version`` raised, the run row
    due at once; a pending reset of a Restart with dependants is cancelled (§3), as read. The dispatcher stops the
    rows. None when a stop mark is set already: a second Stop changes nothing."""
    view = dispatch.run_view(run)
    if view.stop is not None:
        return None
    if not view.active:
        raise ActionRefused(f"{view.execution}: a run that is not active yet takes no Stop")
    stamp = format_utc(now)
    names = {**run_row.VERSION_NAMES, "#status": run_row.STATUS, "#stop": run_row.STOP, "#req": run_row.REQUESTS,
             "#reset": run_row.RESET_DEPENDANTS, "#att": attention.KEY, "#due": attention.DUE, "#ttl": run_row.TTL}
    values = {**run_row.VERSION_RAISE_VALUES, ":active": run_row.ACTIVE, ":now": stamp,
              ":mark": run_row.stop_mark(by=by, reason=reason, at=stamp),
              ":att": attention.attention(attention.WAITING, view.key), ":ttl": retention.ttl(now, retention.HISTORY)}
    expression = f"SET #stop = :mark, {run_row.VERSION_RAISE}, #att = :att, #due = :now, #ttl = :ttl"
    condition = "#status = :active AND attribute_not_exists(#stop)"
    if run_row.RESET_DEPENDANTS in view.requests:
        expression += " REMOVE #req.#reset"
        condition += " AND attribute_exists(#req.#reset)"
    else:
        condition += " AND attribute_not_exists(#req.#reset)"
    return _run_write(view, expression=expression, condition=condition, names=names, values=values, table=table)


def pause_run(run: Mapping[str, Any], *, now: datetime, table: str) -> Optional[Dict[str, Any]]:
    """The console's Pause (§5): an interval opened on the run row, ``run_version`` raised, so no pass that read the
    run row before writes a ``deps_ready`` after (§1). None when the run is paused already."""
    view = dispatch.run_view(run)
    if view.paused:
        return None
    if not view.active or view.stop is not None:
        raise ActionRefused(f"{view.execution}: a run that is not active, or that is stopped, takes no Pause")
    return _pauses_write(view, run, pauses.pause(view.pauses, now), now=now, table=table)


def resume_run(run: Mapping[str, Any], *, now: datetime, table: str) -> Optional[Dict[str, Any]]:
    """The console's Resume (§5): the open interval closed, ``run_version`` raised; the run row's change wakes the
    dispatcher, which decides the held rows. None when the run is not paused."""
    view = dispatch.run_view(run)
    if not view.paused:
        return None
    return _pauses_write(view, run, pauses.resume(view.pauses, now), now=now, table=table)


def _pauses_write(view: dispatch.RunView, run: Mapping[str, Any], intervals: Sequence[pauses.Interval], *,
                  now: datetime, table: str) -> Dict[str, Any]:
    """The run row's intervals as computed from the read, conditional on them being as read."""
    names = {**run_row.VERSION_NAMES, "#status": run_row.STATUS, "#pauses": run_row.PAUSES, "#ttl": run_row.TTL}
    values: Dict[str, Any] = {**run_row.VERSION_RAISE_VALUES, ":active": run_row.ACTIVE,
                              ":pauses": pauses.to_item(intervals), ":ttl": retention.ttl(now, retention.HISTORY)}
    condition = "#status = :active"
    if run_row.PAUSES in run:
        condition += " AND #pauses = :read"
        values[":read"] = run[run_row.PAUSES]
    else:
        condition += " AND attribute_not_exists(#pauses)"
    return _run_write(view, expression=f"SET #pauses = :pauses, {run_row.VERSION_RAISE}, #ttl = :ttl",
                      condition=condition, names=names, values=values, table=table)


def _run_write(view: dispatch.RunView, *, expression: str, condition: str, names: Mapping[str, str],
               values: Mapping[str, Any], table: str) -> Dict[str, Any]:
    return {"TableName": table, "Key": {run_row.TABLE_KEY: {"S": view.key}}, "UpdateExpression": expression,
            "ConditionExpression": condition, "ExpressionAttributeNames": dict(names),
            "ExpressionAttributeValues": row_write.typed(values)}
