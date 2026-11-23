"""What one pass of the dispatcher decides for each row of a run, and for the run (ADR #137 §1, §4, §5).

The dispatcher (``sam/lambdas/coordination/dispatcher.py``) reads the run row, the
rows of its set with their direct upstreams, and the facts a ready row waits
for; these functions say what to write. They are pure and read no clock: ``now``
is the pass's time. Each judges a row from the row itself (§1):

- a waiting row is decided from its upstreams' outcomes (ADR #138) and its
  facts, and written ``deps_ready`` before its start (invariant 3), ``skipped``
  by the rule, ``upstream_failed``, or ``failed`` for an unknown rule, a missing
  upstream row (invariant 11) or a wait past its limit (``deadlines``);
- a ready row with ``wait_before`` counts down first; it is not decided again
  before the countdown ends, and then from its upstreams alone, so its facts'
  ``.within()`` is not judged twice. A countdown whose readiness went is removed;
- a paused run starts nothing: its ready rows wait ``waiting_paused`` and a
  ``deps_ready`` row is not started again (§5); a countdown runs on in a pause;
- a ``deps_ready`` row whose start failed is started again at its next try and
  fails past ``START_FAIL_AFTER``; ``start_failed`` records a failed attempt;
- a ``waiting_decision`` row fails at its decision deadline;
- in the sweeper's passes only, a ``deps_ready`` or ``running`` row whose
  liveness check has come has its ``run_task`` execution judged (``liveness``):
  ended without an outcome, it fails; not visible yet, it is no dead one; a
  ``deps_ready`` row whose execution does not exist is started again;
- a run being stopped has every unfinished row stopped, dependants first (§4),
  and so has a row whose run row is missing (invariant 11);
- the engine stops a run that never became active, or whose execution ended
  before its answer (``engine_stop_cause``, §4);
- once every row is final, the run's outcome answers its token (§4), and the
  summary is written when its counts changed.

Every transition is one of ``transitions.TASK_TRANSITIONS``; the dispatcher's
writes take their condition from it and check the run row's ``run_version``.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..constants import ExecutionStatus, TaskSpecKey, TaskStatus
from . import attention, deadlines, ownership, pauses, run_end, run_row, start_run, sweep, task_row, task_spec
from . import transitions, trigger_rules
from . import decide as dc
from .asset_conditions import Evaluation
from .deadlines import RowTimes
from .times import format_utc, parse_utc

S = TaskStatus
K = TaskSpecKey
_WAITS = frozenset({S.WAITING.value, S.WAITING_PAUSED.value})
#: Why a ready row still waits; the time is the row's ``countdown_end``, not part of the reason (§1).
COUNTING_DOWN = "counting down its wait_before"


@dataclass(frozen=True)
class RunView:
    """The run row as a pass reads it."""
    key: str
    execution: str
    status: str
    version: int
    run_start: str = ""                    # the execution's StartTime as Step Functions gave it
    token: Optional[str] = None
    pauses: Tuple[pauses.Interval, ...] = ()
    stop: Optional[Mapping[str, Any]] = None
    stop_done: bool = False
    step_failure: Optional[Mapping[str, Any]] = None
    task_count: int = 0
    token_order: Optional[int] = None
    answered_order: Optional[int] = None
    summary: Optional[Mapping[str, int]] = None
    date: str = ""                         # the run's date, by which its rows are keyed
    left_at: Optional[str] = None          # when a pass last left rows (§7)
    #: The heavy actions that wait on the run, by kind (run_row.request): a Restart's pointers, a reset (§3).
    requests: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    pipeline: str = ""
    started_at: Optional[datetime] = None  # the execution's StartTime
    execution_arn: str = ""                # the execution's ARN, which the engine's check describes (§4)

    @property
    def active(self) -> bool:
        return self.status == run_row.ACTIVE

    @property
    def paused(self) -> bool:
        return pauses.is_paused(self.pauses)

    @property
    def stopping(self) -> bool:
        """A stop is in progress: the mark is set and the stop is not done (§4)."""
        return self.stop is not None and not self.stop_done

    @property
    def answered(self) -> bool:
        """The current token's answer is marked (§4)."""
        return self.token_order is not None and self.answered_order == self.token_order


def run_view(item: Mapping[str, Any]) -> RunView:
    """The run row read back."""
    summary = item.get(run_row.SUMMARY)
    return RunView(
        key=str(item[run_row.TABLE_KEY]), execution=str(item.get(run_row.EXECUTION, "")),
        status=str(item.get(run_row.STATUS, "")), version=int(item.get(run_row.RUN_VERSION, 0)),
        run_start=str(item.get(run_row.RUN_START, "")), token=item.get(run_row.TOKEN),
        pauses=tuple(pauses.from_item(item.get(run_row.PAUSES) or [])),
        stop=item.get(run_row.STOP), stop_done=bool(item.get(run_row.STOP_DONE)),
        step_failure=item.get(run_row.STEP_FAILURE), task_count=int(item.get(run_row.TASK_COUNT, 0)),
        token_order=_int(item.get(run_row.TOKEN_ORDER)), answered_order=_int(item.get(run_row.ANSWERED)),
        summary={str(k): int(v) for k, v in summary.items()} if isinstance(summary, Mapping) else None,
        date=str(item.get(run_row.RUN_DATE, "")), left_at=item.get(run_row.LEFT_AT),
        requests=_requests(item.get(run_row.REQUESTS)), pipeline=str(item.get(run_row.PIPELINE, "")),
        started_at=_moment(item.get(run_row.STARTED_AT)), execution_arn=str(item.get(run_row.EXECUTION_ARN, "")),
    )


def _requests(value: Any) -> Dict[str, Dict[str, Any]]:
    """The run row's requests by kind; a kind whose value is no map counts as none (Rule 51)."""
    if not isinstance(value, Mapping):
        return {}
    return {str(kind): dict(request) for kind, request in value.items() if isinstance(request, Mapping)}


@dataclass(frozen=True)
class RowView:
    """A task row as a pass reads it: its status, its readiness fields and its times."""
    task_id: str
    key: str
    status: str
    generation: int
    trigger_rule: str
    dependencies: Tuple[str, ...]
    times: RowTimes
    wait_before: timedelta = timedelta(0)
    reasons: Tuple[str, ...] = ()
    due: Optional[datetime] = None
    skip_origin: Optional[str] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None

    @property
    def has_wait_for(self) -> bool:
        return self.times.has_wait_for


def row_view(item: Mapping[str, Any]) -> RowView:
    """A task row read back; its due time is the ``waiting`` or ``decision`` entry it holds, if any."""
    if task_row.READINESS not in item:
        raise ValueError(f"{item.get(task_row.TABLE_KEY)}: a task row of 1.3 carries its readiness fields")
    readiness = task_spec.readiness_from_text(str(item[task_row.READINESS]))
    entry = str(item.get(attention.KEY, ""))
    due = item.get(attention.DUE) if entry.split("#", 1)[0] in (attention.WAITING, attention.DECISION) else None
    return RowView(
        task_id=str(item[task_row.TASK_NAME]), key=str(item[task_row.TABLE_KEY]), status=str(item[task_row.STATUS]),
        generation=int(item[task_row.GENERATION]), trigger_rule=str(readiness[K.TRIGGER_RULE.value]),
        dependencies=tuple(str(d) for d in readiness[K.DEPENDENCIES.value]), times=task_row.row_times(item),
        wait_before=timedelta(seconds=int(readiness.get(K.WAIT_BEFORE.value, 0))),
        reasons=tuple(str(r) for r in item.get(task_row.REASONS) or ()),
        due=parse_utc(str(due)) if due else None, skip_origin=item.get(task_row.SKIP_ORIGIN),
        finished_at=_moment(item.get(task_row.FINISHED_AT)), error=item.get(task_row.ERROR),
    )


def stop_view(item: Mapping[str, Any]) -> RowView:
    """A row read back for its stop alone, when its run row is missing (invariant 11).

    A row that a 1.2 run left in flight has no readiness fields, and may have no
    generation, which then reads as ``ownership.LEGACY``: a stop needs only the
    status and the generation (*Migration*).
    """
    if task_row.READINESS in item:
        return row_view(item)
    status = str(item[task_row.STATUS])
    generation = item.get(task_row.GENERATION)
    return RowView(task_id=str(item.get(task_row.TASK_NAME) or item[task_row.TABLE_KEY]),
                   key=str(item[task_row.TABLE_KEY]), status=status,
                   generation=ownership.LEGACY if generation is None or generation == "" else int(generation),
                   trigger_rule="", dependencies=(), times=RowTimes(status=status), error=item.get(task_row.ERROR))


@dataclass(frozen=True)
class Judgment:
    """What a pass writes for one row: a transition, fields, and its due time after the write.

    ``due`` is the time of the row's ``waiting`` or ``decision`` entry after the
    write (``deadlines.due_time``); the dispatcher writes it when it differs
    from the row's. A ``failed`` row's entry follows from its status (§6).
    ``since`` is set when the judgment ends the row's wait from its upstreams or
    its facts: from when that outcome could have been decided, which the
    sweeper's check of a lost hint reads (§7).
    """
    task_id: str
    transition: Optional[str] = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    removed: Tuple[str, ...] = ()
    due: Optional[datetime] = None
    start: bool = False
    cause: Optional[str] = None
    warning: Optional[str] = None
    since: Optional[datetime] = None

    @property
    def status(self) -> Optional[str]:
        """The row's status after the write, when the judgment moves it."""
        return transitions.task_transition(self.transition).target if self.transition else None

    @property
    def leaves_wait(self) -> bool:
        """The row leaves the wait, so its ``asset:`` pointers go (Rule 40, §7)."""
        return self.status is not None and self.status not in _WAITS


def needs_assets(row: RowView, upstreams: Mapping[str, Optional[RowView]]) -> bool:
    """Whether a pass must evaluate the row's facts before ``judge``: its upstreams allow it and it does not
    count down (a countdown is not judged again, §1)."""
    if row.status not in _WAITS or not row.has_wait_for or row.times.countdown_end is not None:
        return False
    if any(upstreams.get(d) is None for d in row.dependencies):
        return False
    return _rule(row, upstreams).verdict == trigger_rules.READY


def judge(row: RowView, upstreams: Mapping[str, Optional[RowView]], run: RunView, *, now: datetime,
          assets: Optional[Evaluation] = None) -> Judgment:
    """What a pass over an active run that is not being stopped writes for ``row``.

    ``upstreams`` maps each of the row's dependencies to its row, None when the row
    is missing. ``assets`` is the evaluation of its facts when ``needs_assets``.
    """
    if row.status in _WAITS:
        return _wait(row, upstreams, run, now, assets)
    if row.status == S.DEPS_READY.value:
        return _start(row, run, now)
    if row.status == S.WAITING_DECISION.value:
        deadline = deadlines.decision_deadline(row.times, run.pauses)
        if deadline is not None and deadline <= now:
            return _final(row, "fail_undecided", now,
                          cause=f"no decision came within its decision timeout of {row.times.decision_timeout}")
        return Judgment(row.task_id, due=deadline)
    return Judgment(row.task_id, due=deadlines.due_time(row.times, run.pauses))


def rule_verdict(row: RowView, upstreams: Mapping[str, Optional[RowView]]) -> str:
    """What the row's trigger rule gives from ``upstreams`` as they are (trigger_rules' verdicts): the reset of a
    Restart with dependants compares it with the decision the row holds (actions.reset_set)."""
    return _rule(row, upstreams).verdict


def _rule(row: RowView, upstreams: Mapping[str, Optional[RowView]]) -> trigger_rules.DepEvaluation:
    ups = [upstreams[d] for d in row.dependencies]
    return trigger_rules.evaluate(row.trigger_rule, [u.status for u in ups if u],
                                  [u.skip_origin for u in ups if u])


def _wait(row: RowView, upstreams: Mapping[str, Optional[RowView]], run: RunView, now: datetime,
          assets: Optional[Evaluation]) -> Judgment:
    missing = sorted(d for d in row.dependencies if upstreams.get(d) is None)
    if missing:
        cause = f"the row of its upstream {missing[0]} is missing"
        return _final(row, "fail_wait", now, cause=cause, warning=f"{row.task_id}: {cause}")
    ups = [upstreams[d] for d in row.dependencies]
    statuses = [u.status for u in ups if u]
    origins = [u.skip_origin for u in ups if u]
    evaluation = trigger_rules.evaluate(row.trigger_rule, statuses, origins)
    countdown = row.times.countdown_end
    if countdown is not None:
        if now < countdown:
            return Judgment(row.task_id, due=countdown)    # not decided again during its countdown (§1)
        if evaluation.verdict == trigger_rules.READY:
            return _ready(row, run, now)                   # from its upstreams alone: no .within() again
    if evaluation.verdict == trigger_rules.FAIL:
        return _final(row, "fail_wait", now, cause=evaluation.reason)
    allowed = trigger_rules.enabled_at(row.trigger_rule, statuses, [u.finished_at for u in ups if u], origins)
    decided = deadlines.enabled_since(allowed, row.times.waiting_since or now)
    if evaluation.verdict == trigger_rules.SKIP:
        return _final(row, "skip_by_rule", now, reasons=(evaluation.reason,), since=decided)
    if evaluation.verdict == trigger_rules.UPSTREAM_FAILED:
        return _final(row, "block", now, reasons=(evaluation.reason,), since=decided)
    enabled = decided if evaluation.verdict == trigger_rules.READY else None
    decision = dc.decide(dc.TaskView(row.status, row.trigger_rule, row.dependencies, row.has_wait_for),
                         dep_statuses=statuses, skip_origins=origins, assets=assets)
    if decision.verdict == dc.READY and enabled is not None:
        ready_at = max(enabled, assets.enabled_at) if assets is not None and assets.enabled_at else enabled
        end = deadlines.countdown_end(ready_at, row.wait_before)
        if end is not None and end > now:
            return _waiting(row, run, now, enabled=enabled, reasons=(COUNTING_DOWN,), countdown=end)
        return _ready(row, run, now, enabled=enabled, since=ready_at)
    return _waiting(row, run, now, enabled=enabled, reasons=decision.reasons, countdown=None)


def _waiting(row: RowView, run: RunView, now: datetime, *, enabled: Optional[datetime],
             reasons: Sequence[str], countdown: Optional[datetime]) -> Judgment:
    """The row waits on: record what changed, unless its wait passed its limit."""
    times = replace(row.times, status=S.WAITING.value, enabled_at=enabled, countdown_end=countdown)
    limit = deadlines.wait_deadline(times, run.pauses)
    if limit is not None and limit <= now:
        return _final(row, "fail_wait", now,
                      cause=f"it waited past its limit at {format_utc(limit)}: {'; '.join(reasons)}")
    fields: Dict[str, Any] = {}
    removed: List[str] = []
    transition = "release" if row.status == S.WAITING_PAUSED.value else None
    if transition:
        fields[task_row.STATUS_SINCE] = format_utc(now)
    _set_time(fields, removed, task_row.ENABLED_AT, enabled, row.times.enabled_at)
    _set_time(fields, removed, task_row.COUNTDOWN_END, countdown, row.times.countdown_end)
    if tuple(reasons) != row.reasons:
        fields[task_row.REASONS] = list(reasons)
    return Judgment(row.task_id, transition, fields, tuple(removed), due=deadlines.due_time(times, run.pauses))


def _ready(row: RowView, run: RunView, now: datetime, *, enabled: Optional[datetime] = None,
           since: Optional[datetime] = None) -> Judgment:
    """The row may start: ``deps_ready`` and its start, or ``waiting_paused`` while the run is paused (§5).

    ``since`` is when it became ready; a countdown's end gives none, since the sweeper is how a countdown ends.
    """
    fields: Dict[str, Any] = {}
    removed: List[str] = []
    if enabled is not None:
        _set_time(fields, removed, task_row.ENABLED_AT, enabled, row.times.enabled_at)
    if run.paused:
        if row.status == S.WAITING_PAUSED.value:
            return Judgment(row.task_id, fields=fields, removed=tuple(removed))
        fields[task_row.STATUS_SINCE] = format_utc(now)
        return Judgment(row.task_id, "hold", fields, tuple(removed + _present(row, (task_row.REASONS,))))
    fields[task_row.STATUS_SINCE] = format_utc(now)
    removed += _present(row, (task_row.REASONS, task_row.COUNTDOWN_END, task_row.START_FAILED_FIRST,
                              task_row.START_FAILED_LAST, task_row.START_FAILURES, task_row.LAST_CHECK,
                              task_row.CHECKS, task_row.ERROR))
    times = RowTimes(status=S.DEPS_READY.value, status_since=now)
    return Judgment(row.task_id, "ready", fields, tuple(removed), due=deadlines.due_time(times, run.pauses),
                    start=True, since=since)


def _start(row: RowView, run: RunView, now: datetime) -> Judgment:
    """A ``deps_ready`` row: started again at its next try, failed past its deadline; nothing in a pause."""
    if run.paused:
        return Judgment(row.task_id)                       # start nothing, not even a failed start (§5)
    start = deadlines.start_deadlines(row.times, run.pauses)
    if start is None:
        return Judgment(row.task_id, due=deadlines.due_time(row.times, run.pauses))
    if start.fail_at is not None and start.fail_at <= now:
        return _final(row, "fail_start", now,
                      cause=f"its start kept failing for {deadlines.START_FAIL_AFTER}: {row.error or 'no error kept'}")
    return Judgment(row.task_id, start=start.next_try <= now, due=start.next_try)


#: How a start attempt failed (§5, §7).
TRANSIENT = "transient"   # throttling, a 5xx, a timeout: tried again after a backoff
DENIED = "denied"         # AccessDeniedException: counts toward no deadline; a warning, and the next check
REFUSED = "refused"       # refused for good, such as an input over 256 KB: the row fails at once
START_FAILURES = frozenset({TRANSIENT, DENIED, REFUSED})


def start_failed(row: RowView, run: RunView, *, now: datetime, error: str, kind: str) -> Judgment:
    """What a failed start attempt writes on a ``deps_ready`` row.

    A transient failure counts toward the row's deadlines from its first failed
    attempt, warns once when an attempt is the first past ``START_WARN_AFTER`` and
    fails the row past ``START_FAIL_AFTER`` (§5). An ``AccessDeniedException`` counts
    toward neither and fails no row: it warns, and the row waits for its next check
    (§7). A start refused for good fails the row with the cause.
    """
    if kind not in START_FAILURES:
        raise ValueError(f"unknown kind of failed start {kind!r}")
    if kind == REFUSED:
        return _final(row, "fail_start", now, cause=f"its start was refused: {error}")
    if kind == DENIED:
        return Judgment(row.task_id, due=deadlines.due_time(row.times, run.pauses),
                        warning=f"{row.task_id}: run_task could not be started, access denied: {error}")
    first = row.times.start_failed_first or now
    times = replace(row.times, start_failed_first=first, start_failed_last=now,
                    start_failures=row.times.start_failures + 1)
    deadline = deadlines.start_deadlines(times, run.pauses)
    assert deadline is not None    # one failure is recorded in `times`
    if deadline.fail_at is not None and deadline.fail_at <= now:
        return _final(row, "fail_start", now, cause=f"its start kept failing for {deadlines.START_FAIL_AFTER}: {error}")
    before = row.times.start_failed_last
    warn = deadline.warn_at is not None and deadline.warn_at <= now and (before is None or before < deadline.warn_at)
    fields = {task_row.START_FAILED_FIRST: format_utc(first), task_row.START_FAILED_LAST: format_utc(now),
              task_row.START_FAILURES: times.start_failures, task_row.ERROR: error}
    return Judgment(row.task_id, fields=fields, due=deadlines.due_time(times, run.pauses),
                    warning=(f"{row.task_id}: run_task has failed to start since {format_utc(first)}: {error}"
                             if warn else None))


def started(row: RowView, run: RunView, *, now: datetime) -> Judgment:
    """A start that went through, or found its execution there already, on a ``deps_ready`` row whose earlier
    attempts failed: the failures go, and its liveness counts from this start (§1, §5). Nothing for any other row."""
    if row.status != S.DEPS_READY.value or row.times.start_failures < 1:
        return Judgment(row.task_id, due=row.due)
    times = replace(row.times, start_failed_first=None, start_failed_last=None, start_failures=0, status_since=now,
                    last_check=None, checks=0)
    removed = _present(row, (task_row.START_FAILED_FIRST, task_row.START_FAILED_LAST, task_row.START_FAILURES,
                             task_row.LAST_CHECK, task_row.CHECKS, task_row.ERROR))
    return Judgment(row.task_id, fields={task_row.STATUS_SINCE: format_utc(now)}, removed=tuple(removed),
                    due=deadlines.due_time(times, run.pauses))


#: The statuses DescribeExecution gives an execution that has ended.
ENDED = frozenset({"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"})


@dataclass(frozen=True)
class Execution:
    """What DescribeExecution said of an execution; ``status`` None: it does not exist."""
    status: Optional[str]
    error: Optional[str] = None
    cause: Optional[str] = None

    @property
    def ended(self) -> bool:
        return self.status in ENDED

    def account(self) -> str:
        """How it ended, in words: its status, then its error and cause when it has them."""
        detail = ": ".join(str(part) for part in (self.error, self.cause) if part)
        return f"{self.status or 'missing'}" + (f" ({detail})" if detail else "")


def liveness_due(row: RowView, run: RunView, *, now: datetime) -> bool:
    """Whether a pass of the sweeper judges the row's ``run_task`` execution now (§1, §7).

    A ``running`` row is judged at its liveness check; a ``deps_ready`` row too,
    unless a start of it failed (its next try is due instead) or its run is
    paused, which holds its start.
    """
    if row.status == S.DEPS_READY.value:
        if run.paused or row.times.start_failures > 0:
            return False
    elif row.status != S.RUNNING.value:
        return False
    check = deadlines.liveness_check(row.times)
    return check is not None and check <= now


def liveness(row: RowView, run: RunView, *, now: datetime, execution: Execution) -> Judgment:
    """What the sweeper's pass writes after it judged the row's ``run_task`` execution (§1, §3, invariant 10).

    - It ended, and the row still waits for its outcome: the row fails with how it ended.
    - It does not exist within ``sweep.EXECUTION_VISIBLE_AFTER`` of the row's status: it may
      not be visible yet, so the row is no dead one.
    - It does not exist after that: a ``deps_ready`` row is started again under its derived
      name (invariant 3), and a ``running`` row, which ``run_task`` itself wrote, fails.
    - Otherwise it runs. Each check that does not end the row records its time and number,
      from which the next one backs off.
    """
    times = replace(row.times, last_check=now, checks=row.times.checks + 1)
    checked = Judgment(row.task_id, fields={task_row.LAST_CHECK: format_utc(now), task_row.CHECKS: times.checks},
                       due=deadlines.due_time(times, run.pauses))
    if execution.ended:
        return _final(row, "fail_dead", now,
                      cause=f"its run_task execution ended {execution.account()} without recording an outcome")
    if execution.status is not None:
        return checked
    since = row.times.status_since
    if since is None or now - since < sweep.EXECUTION_VISIBLE_AFTER:
        return checked
    if row.status == S.DEPS_READY.value:
        return replace(checked, start=True)
    return _final(row, "fail_dead", now, cause="its run_task execution does not exist")


def engine_check_due(run: RunView, *, now: datetime, finished: bool) -> bool:
    """Whether the run's check reads its execution: no stop mark, no answer marked and a row not final, and
    either ``active``, or ``creating`` past ``Start_Run``'s retry window (§4). Only then may the engine stop it."""
    if run.stop is not None or run.answered or finished or run.started_at is None:
        return False
    if run.status == run_row.CREATING:
        return now - run.started_at >= start_run.retry_window()
    return run.status == run_row.ACTIVE


def engine_stop_cause(run: RunView, *, now: datetime, execution: Optional[Execution], finished: bool) -> Optional[str]:
    """Why the engine stops the run at its check, or None (§4).

    ``execution`` is what DescribeExecution said of the run's execution; None when
    it could not be read, which stops nothing (invariant 10). No run is stopped
    that carries a stop mark, whose answer is marked, or whose rows are all final:
    its end answers it.

    - A run still ``creating`` after ``Start_Run``'s retry window never became
      active; the cause names how its execution ended, if it did.
    - An ``active`` run whose execution ended before the answer, or does not
      exist once ``sweep.EXECUTION_VISIBLE_AFTER`` has passed since its start.
    """
    if execution is None or run.started_at is None or not engine_check_due(run, now=now, finished=finished):
        return None
    if run.status == run_row.CREATING:
        ended = f"; its execution {execution.account()}" if execution.ended or execution.status is None else ""
        return f"Start_Run did not make the run active within its retry window of {start_run.retry_window()}{ended}"
    age = now - run.started_at
    if execution.ended:
        return f"its execution ended {execution.account()} before the run's answer"
    if execution.status is None and age >= sweep.EXECUTION_VISIBLE_AFTER:
        return "its execution does not exist"
    return None


def stop(row: RowView, *, now: datetime, cause: str) -> Judgment:
    """A stop of the run (§4), or of a row whose run row is missing (invariant 11): ``stopped`` unless final."""
    if row.status in transitions.FINAL:
        return Judgment(row.task_id)
    return _final(row, "stop", now, cause=cause)


def stop_order(rows: Mapping[str, RowView]) -> List[str]:
    """The rows in the order a stop writes them: every dependant before its upstreams (§4).

    Iterative, with the dependants counted once: a chain as long as a pipeline may be (2,000 tasks) never
    meets Python's recursion limit (polyris/CLAUDE.md, "Graph traversal")."""
    dependants: Dict[str, int] = {name: 0 for name in rows}
    for row in rows.values():
        for upstream in set(row.dependencies):
            if upstream in dependants:
                dependants[upstream] += 1
    ready = [name for name, count in dependants.items() if count == 0]
    heapq.heapify(ready)
    order: List[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for upstream in set(rows[name].dependencies):
            if upstream in dependants:
                dependants[upstream] -= 1
                if dependants[upstream] == 0:
                    heapq.heappush(ready, upstream)
    return order + sorted(set(rows) - set(order))         # a cycle cannot come from a deploy; its rows go last


def upstreams_first(names: Iterable[str], dependencies: Mapping[str, Sequence[str]]) -> List[str]:
    """``names`` in an order where each comes after its upstreams among them, so a pass decides a row after the
    rows it reads. Iterative, like ``stop_order``."""
    chosen = set(names)
    waiting = {name: len(set(dependencies.get(name, ())) & chosen) for name in chosen}
    after: Dict[str, List[str]] = {name: [] for name in chosen}
    for name in chosen:
        for upstream in set(dependencies.get(name, ())) & chosen:
            after[upstream].append(name)
    ready = [name for name, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    order: List[str] = []
    while ready:
        name = heapq.heappop(ready)
        order.append(name)
        for dependant in after[name]:
            waiting[dependant] -= 1
            if waiting[dependant] == 0:
                heapq.heappush(ready, dependant)
    return order + sorted(chosen - set(order))            # a cycle cannot come from a deploy; its rows go last


def undecidable(row: RowView, *, now: datetime, cause: str) -> Judgment:
    """What a pass writes on a row it could not decide (§3, Rule 51): a waiting row fails with the cause, as on a
    CoordinationError, so its run goes on; any other row keeps its state, and the cause goes to the bell."""
    if row.status in transitions.task_transition("fail_wait").sources:
        return _final(row, "fail_wait", now, cause=cause, warning=f"{row.task_id}: {cause}")
    return Judgment(row.task_id, due=row.due, warning=f"{row.task_id}: {cause}")


def _final(row: RowView, transition: str, now: datetime, *, cause: Optional[str] = None,
           reasons: Optional[Sequence[str]] = None, warning: Optional[str] = None,
           since: Optional[datetime] = None) -> Judgment:
    fields: Dict[str, Any] = {task_row.STATUS_SINCE: format_utc(now), task_row.FINISHED_AT: format_utc(now)}
    removed = _present(row, (task_row.COUNTDOWN_END,))
    if cause is not None:
        fields[task_row.ERROR] = cause
    if reasons is not None:
        fields[task_row.REASONS] = list(reasons)
    elif row.reasons:
        removed.append(task_row.REASONS)
    return Judgment(row.task_id, transition, fields, tuple(removed), cause=cause, warning=warning, since=since)


@dataclass(frozen=True)
class RunEnd:
    """What a full pass decides for the run (§4)."""
    summary: Dict[str, int]
    outcome: Optional[str]
    cause: Optional[str] = None
    missing: Tuple[str, ...] = ()

    @property
    def finished(self) -> bool:
        return self.outcome is not None


def run_outcome(run: RunView, statuses: Mapping[str, Optional[str]]) -> RunEnd:
    """The run's summary and, once every row is final, its outcome and a failed run's cause.

    ``statuses`` maps every task of the run's spec version to its status after
    the pass, or None for a row that is missing: it counts as failed (invariant 11).
    """
    missing = tuple(sorted(t for t, s in statuses.items() if s is None))
    final = {t: (s if s is not None else S.FAILED.value) for t, s in statuses.items()}
    counts = run_end.summary(final.values())
    step = run.step_failure
    outcome = run_end.outcome(counts, task_count=run.task_count, step_failed=step is not None)
    cause = None
    if outcome is not None and outcome != ExecutionStatus.SUCCESS:
        failed = [t for t, s in final.items() if s in transitions.FINAL_FAILURES]
        cause = run_end.failure_cause(failed, step_failure=str(step.get(run_row.STEP_MARK_STEP)) if step else None)
    return RunEnd(counts, outcome, cause, missing)


def needs_answer(run: RunView, end: RunEnd) -> bool:
    """The run finished and its current token has no answer marked (§4)."""
    return end.finished and not run.answered


def summary_changed(run: RunView, end: RunEnd) -> bool:
    return dict(run.summary or {}) != end.summary


def _set_time(fields: Dict[str, Any], removed: List[str], name: str, value: Optional[datetime],
              stored: Optional[datetime]) -> None:
    if value == stored:
        return
    if value is None:
        removed.append(name)
    else:
        fields[name] = format_utc(value)


def _present(row: RowView, names: Sequence[str]) -> List[str]:
    """The fields among ``names`` that the row holds, so a write removes only what is there."""
    held = {task_row.REASONS: bool(row.reasons), task_row.COUNTDOWN_END: row.times.countdown_end is not None,
            task_row.START_FAILED_FIRST: row.times.start_failed_first is not None,
            task_row.START_FAILED_LAST: row.times.start_failed_last is not None,
            task_row.START_FAILURES: row.times.start_failures > 0, task_row.LAST_CHECK: row.times.last_check is not None,
            task_row.CHECKS: row.times.checks > 0, task_row.ERROR: row.error is not None}
    return [name for name in names if held.get(name)]


def _moment(value: Any) -> Optional[datetime]:
    return parse_utc(str(value)) if value else None


def _int(value: Any) -> Optional[int]:
    return int(value) if value is not None else None
