"""What one pass of the dispatcher decides for each row of a run, and for the run (ADR #137 §1, §4, §5).

The dispatcher (``sam/lambdas/coordination/reconcile.py``) reads the run row, the
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
- a run being stopped has every unfinished row stopped, dependants first (§4),
  and so has a row whose run row is missing (invariant 11);
- once every row is final, the run's outcome answers its token (§4), and the
  summary is written when its counts changed.

Every transition is one of ``transitions.TASK_TRANSITIONS``; the dispatcher's
writes take their condition from it and check the run row's ``run_version``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..constants import ExecutionStatus, SkipOrigin, TaskSpecKey, TaskStatus
from . import attention, deadlines, pauses, run_end, run_row, task_row, task_spec, transitions, trigger_rules
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
    pauses: Tuple[pauses.Interval, ...] = ()
    stop: Optional[Mapping[str, Any]] = None
    stop_done: bool = False
    step_failure: Optional[Mapping[str, Any]] = None
    task_count: int = 0
    token_order: Optional[int] = None
    answered_order: Optional[int] = None
    summary: Optional[Mapping[str, int]] = None

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
        pauses=tuple(pauses.from_item(item.get(run_row.PAUSES) or [])),
        stop=item.get(run_row.STOP), stop_done=bool(item.get(run_row.STOP_DONE)),
        step_failure=item.get(run_row.STEP_FAILURE), task_count=int(item.get(run_row.TASK_COUNT, 0)),
        token_order=_int(item.get(run_row.TOKEN_ORDER)), answered_order=_int(item.get(run_row.ANSWERED)),
        summary={str(k): int(v) for k, v in summary.items()} if isinstance(summary, Mapping) else None,
    )


@dataclass(frozen=True)
class RowView:
    """A task row as a pass reads it: its status, its decision fields and its times."""
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
    decision = task_spec.decision_from_text(str(item[task_row.DECISION]))
    entry = str(item.get(attention.KEY, ""))
    due = item.get(attention.DUE) if entry.split("#", 1)[0] in (attention.WAITING, attention.DECISION) else None
    return RowView(
        task_id=str(item[task_row.TASK_NAME]), key=str(item[task_row.TABLE_KEY]), status=str(item[task_row.STATUS]),
        generation=int(item[task_row.GENERATION]), trigger_rule=str(decision[K.TRIGGER_RULE.value]),
        dependencies=tuple(str(d) for d in decision[K.DEPENDENCIES.value]), times=task_row.row_times(item),
        wait_before=timedelta(seconds=int(decision.get(K.WAIT_BEFORE.value, 0))),
        reasons=tuple(str(r) for r in item.get(task_row.REASONS) or ()),
        due=parse_utc(str(due)) if due else None, skip_origin=item.get(task_row.SKIP_ORIGIN),
        finished_at=_moment(item.get(task_row.FINISHED_AT)), error=item.get(task_row.ERROR),
    )


@dataclass(frozen=True)
class Judgment:
    """What a pass writes for one row: a transition, fields, and its due time after the write.

    ``due`` is the time of the row's ``waiting`` or ``decision`` entry after the
    write (``deadlines.due_time``); the dispatcher writes it when it differs
    from the row's. A ``failed`` row's entry follows from its status (§6).
    """
    task_id: str
    transition: Optional[str] = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    removed: Tuple[str, ...] = ()
    due: Optional[datetime] = None
    start: bool = False
    cause: Optional[str] = None
    warning: Optional[str] = None

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
    if evaluation.verdict == trigger_rules.SKIP:
        return _final(row, "skip_by_rule", now, reasons=(evaluation.reason,))
    if evaluation.verdict == trigger_rules.UPSTREAM_FAILED:
        return _final(row, "block", now, reasons=(evaluation.reason,))
    enabled: Optional[datetime] = None
    if evaluation.verdict == trigger_rules.READY:
        since = trigger_rules.enabled_at(row.trigger_rule, statuses, [u.finished_at for u in ups if u], origins)
        enabled = deadlines.enabled_since(since, row.times.waiting_since or now)
    decision = dc.decide(dc.TaskView(row.status, False, row.trigger_rule, row.dependencies, row.has_wait_for),
                         dep_statuses=statuses, skip_origins=origins, assets=assets)
    if decision.verdict == dc.READY and enabled is not None:
        ready_at = max(enabled, assets.enabled_at) if assets is not None and assets.enabled_at else enabled
        end = deadlines.countdown_end(ready_at, row.wait_before)
        if end is not None and end > now:
            return _waiting(row, run, now, enabled=enabled, reasons=(COUNTING_DOWN,), countdown=end)
        return _ready(row, run, now, enabled=enabled)
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


def _ready(row: RowView, run: RunView, now: datetime, *, enabled: Optional[datetime] = None) -> Judgment:
    """The row may start: ``deps_ready`` and its start, or ``waiting_paused`` while the run is paused (§5)."""
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
                    start=True)


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


def stop(row: RowView, *, now: datetime, cause: str) -> Judgment:
    """A stop of the run (§4), or of a row whose run row is missing (invariant 11): ``stopped`` unless final."""
    if row.status in transitions.FINAL:
        return Judgment(row.task_id)
    return _final(row, "stop", now, cause=cause)


def stop_order(rows: Mapping[str, RowView]) -> List[str]:
    """The rows in the order a stop writes them: every dependant before its upstreams (§4)."""
    order: List[str] = []
    seen: set = set()

    def visit(task_id: str) -> None:
        if task_id in seen:
            return
        seen.add(task_id)
        for dependant in sorted(t for t, r in rows.items() if task_id in r.dependencies):
            visit(dependant)
        order.append(task_id)

    for task_id in sorted(rows):
        visit(task_id)
    return order


def _final(row: RowView, transition: str, now: datetime, *, cause: Optional[str] = None,
           reasons: Optional[Sequence[str]] = None, warning: Optional[str] = None) -> Judgment:
    fields: Dict[str, Any] = {task_row.STATUS_SINCE: format_utc(now), task_row.FINISHED_AT: format_utc(now)}
    removed = _present(row, (task_row.COUNTDOWN_END,))
    if transition == "skip_by_rule":
        fields[task_row.SKIP_ORIGIN] = SkipOrigin.RULE
    if cause is not None:
        fields[task_row.ERROR] = cause
    if reasons is not None:
        fields[task_row.REASONS] = list(reasons)
    elif row.reasons:
        removed.append(task_row.REASONS)
    return Judgment(row.task_id, transition, fields, tuple(removed), cause=cause, warning=warning)


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
