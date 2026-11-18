"""A run's pause intervals, which its run row keeps (ADR #137 §5, D91).

Pause opens an interval and Resume closes it. The row keeps at most ten: a
Pause past ten merges the two oldest into one, which can only move a deadline
later. A pause's time counts toward no waiting limit: the deadline function
moves each deadline by the paused time inside its wait (``paused_within``).
Only the sweeper's lost-hint check reads the latest resume (``resumed_at``).

In the row an interval is a list of epoch seconds, ``[start, end]``, or
``[start]`` while it is open: the run row stays under 4 KB (§3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, List, Optional, Sequence, Tuple

MAX_INTERVALS = 10


@dataclass(frozen=True)
class Interval:
    start: datetime
    #: None while the run is still paused.
    end: Optional[datetime] = None


Intervals = Tuple[Interval, ...]


def from_item(value: Any) -> Intervals:
    """The intervals a run row holds; a missing attribute is no pause."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"pause intervals must be a list, got {type(value).__name__}")
    intervals = []
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) not in (1, 2):
            raise ValueError(f"a pause interval must be [start] or [start, end], got {item!r}")
        start, *end = (_moment(v) for v in item)
        intervals.append(Interval(start, end[0] if end else None))
    if any(i.end is None for i in intervals[:-1]):
        raise ValueError("only the latest pause interval may be open")
    return tuple(intervals)


def to_item(intervals: Sequence[Interval]) -> List[List[int]]:
    """The intervals as the run row stores them."""
    return [[_epoch(i.start)] + ([] if i.end is None else [_epoch(i.end)]) for i in intervals]


def is_paused(intervals: Sequence[Interval]) -> bool:
    return bool(intervals) and intervals[-1].end is None


def pause(intervals: Sequence[Interval], now: datetime) -> Intervals:
    """The intervals after a Pause at ``now``; a paused run stays as it is."""
    if is_paused(intervals):
        return tuple(intervals)
    start = max(now, intervals[-1].end) if intervals and intervals[-1].end is not None else now
    opened = list(intervals) + [Interval(start)]
    if len(opened) > MAX_INTERVALS:
        opened[0:2] = [Interval(opened[0].start, opened[1].end)]
    return tuple(opened)


def resume(intervals: Sequence[Interval], now: datetime) -> Intervals:
    """The intervals after a Resume at ``now``; a run that is not paused stays as it is."""
    if not is_paused(intervals):
        return tuple(intervals)
    last = intervals[-1]
    return tuple(intervals[:-1]) + (Interval(last.start, max(now, last.start)),)


def paused_within(intervals: Sequence[Interval], start: datetime, end: datetime) -> timedelta:
    """How much of ``[start, end]`` the run spent paused; an open interval lasts until ``end``."""
    total = timedelta(0)
    for interval in intervals:
        overlap = min(interval.end or end, end) - max(interval.start, start)
        if overlap > timedelta(0):
            total += overlap
    return total


def resumed_at(intervals: Sequence[Interval]) -> Optional[datetime]:
    """When the run was last resumed, if it ever was."""
    closed = [i.end for i in intervals if i.end is not None]
    return max(closed) if closed else None


def _moment(value: Any) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"a pause time must be epoch seconds, got {value!r}")
    return datetime.fromtimestamp(int(value), tz=timezone.utc)


def _epoch(moment: datetime) -> int:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(moment.timestamp())
