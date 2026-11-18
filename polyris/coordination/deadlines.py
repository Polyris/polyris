"""A task row's deadlines and its next due time (ADR #137 §5, §9, invariant 9; Principle #48).

Both are pure: they read the row's own times, the enabling time of its
upstreams and the run's pause intervals, never the clock. So each pass judges a
row from the row itself and writes its due time only when it changes (§1). The
clauses, each with its test:

- waiting for tasks of the same run has no limit of its own;
- waiting for ``wait_for`` facts lasts ``WAIT_FOR_LIMIT`` from when the task's
  own dependencies allowed it (``trigger_rules.enabled_at``), whose failures
  count from when they became final;
- ``orchestration_timeout``, when set, caps the whole wait until the row is
  ready, counted from the start of the wait, which a Restart begins anew;
- the paused time inside a wait moves its deadline, and in a pause a limit
  has no due time;
- a ready row's countdown (``wait_before``) ends at a time no pause moves, and
  no limit applies to the row while it counts down;
- a decision deadline is the decision timeout in force when the wait for the
  decision began, moved by the paused time; in a pause it falls due far in the
  future, where the bell still reads it;
- a start that keeps failing warns at ``START_WARN_AFTER`` and fails at
  ``START_FAIL_AFTER``, both counted from its first failed attempt and moved
  by paused time; an ``AccessDeniedException`` counts toward neither, so the
  caller records no failed attempt for it (§7);
- a ``deps_ready`` row whose start did not fail, and a ``running`` row, are
  checked on the backoff of ``attention.next_due``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from ..constants import TaskStatus
from . import attention
from .pauses import Interval, is_paused

S = TaskStatus

WAIT_FOR_LIMIT = timedelta(days=7)
START_WARN_AFTER = timedelta(minutes=15)
START_FAIL_AFTER = timedelta(hours=6)
#: The next try of a start that failed: after each failed attempt, at most 5 minutes apart.
START_BACKOFF = (timedelta(seconds=30), timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=5))
#: Where a decision deadline falls due while its run is paused.
FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class RowTimes:
    """What a task row records that its deadlines read."""
    status: str
    #: When the wait began, and again at each Restart.
    waiting_since: Optional[datetime] = None
    #: When its dependencies first allowed the outcome (``trigger_rules.enabled_at``); None while they wait.
    enabled_at: Optional[datetime] = None
    has_wait_for: bool = False
    orchestration_timeout: Optional[timedelta] = None
    #: The end of a ready row's countdown, which the row records.
    countdown_end: Optional[datetime] = None
    start_failed_first: Optional[datetime] = None
    start_failed_last: Optional[datetime] = None
    start_failures: int = 0
    #: When the row became ``deps_ready`` or ``running``, from which its liveness checks count.
    status_since: Optional[datetime] = None
    checks: int = 0
    decision_since: Optional[datetime] = None
    decision_timeout: Optional[timedelta] = None


@dataclass(frozen=True)
class StartDeadlines:
    next_try: datetime
    warn_at: Optional[datetime]
    fail_at: Optional[datetime]


def moved(start: datetime, limit: timedelta, intervals: Sequence[Interval]) -> Optional[datetime]:
    """``start + limit``, moved by the paused time that falls inside the wait; None while a pause holds it."""
    deadline = start + limit
    for interval in intervals:
        if interval.start >= deadline:
            break
        if interval.end is None:
            return None
        begin = max(interval.start, start)
        if interval.end > begin:
            deadline += interval.end - begin
    return deadline


def countdown_end(enabled_at: datetime, wait_before: timedelta) -> Optional[datetime]:
    """When a ready row's countdown ends, which the row records; None without a countdown."""
    return enabled_at + wait_before if wait_before > timedelta(0) else None


def wait_deadline(row: RowTimes, intervals: Sequence[Interval]) -> Optional[datetime]:
    """When a waiting row fails for its wait: the earliest of its limits, or None if it has none now."""
    if row.countdown_end is not None:
        return None
    candidates = []
    if row.orchestration_timeout is not None and row.waiting_since is not None:
        candidates.append(moved(row.waiting_since, row.orchestration_timeout, intervals))
    if row.has_wait_for and row.enabled_at is not None:
        candidates.append(moved(row.enabled_at, WAIT_FOR_LIMIT, intervals))
    known = [c for c in candidates if c is not None]
    return min(known) if known else None


def decision_deadline(row: RowTimes, intervals: Sequence[Interval]) -> Optional[datetime]:
    """When a row that waits for a decision fails; ``FAR_FUTURE`` while a pause holds the deadline."""
    if row.decision_since is None or row.decision_timeout is None:
        return None
    deadline = moved(row.decision_since, row.decision_timeout, intervals)
    return FAR_FUTURE if deadline is None else deadline


def start_deadlines(row: RowTimes, intervals: Sequence[Interval]) -> Optional[StartDeadlines]:
    """When a start that failed is tried again, warns and fails; None if no attempt failed."""
    if row.start_failures < 1 or row.start_failed_first is None or row.start_failed_last is None:
        return None
    backoff = START_BACKOFF[min(row.start_failures, len(START_BACKOFF)) - 1]
    return StartDeadlines(row.start_failed_last + backoff,
                          moved(row.start_failed_first, START_WARN_AFTER, intervals),
                          moved(row.start_failed_first, START_FAIL_AFTER, intervals))


def due_time(row: RowTimes, intervals: Sequence[Interval]) -> Optional[datetime]:
    """The row's next due time in ``attention-index``, or None when it has no time of its own (invariant 9).

    A row that waits only for other rows, or for a pause to end, has none: their
    changes or the Resume wake it, and its run row's check covers it.
    """
    if row.status == S.WAITING.value:
        if row.countdown_end is not None:
            return row.countdown_end
        return wait_deadline(row, intervals)
    if row.status == S.DEPS_READY.value:
        if is_paused(intervals):
            return None
        start = start_deadlines(row, intervals)
        if start is not None:
            return start.next_try
        return _check(row)
    if row.status == S.RUNNING.value:
        return _check(row)
    if row.status == S.WAITING_DECISION.value:
        return decision_deadline(row, intervals)
    return None


def _check(row: RowTimes) -> Optional[datetime]:
    return attention.next_due(row.status_since, row.checks) if row.status_since is not None else None
