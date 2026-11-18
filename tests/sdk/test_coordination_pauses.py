"""A run's pause intervals (ADR #137 §5, D91)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from polyris.coordination import pauses as p

T0 = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)


def at(minutes):
    return T0 + timedelta(minutes=minutes)


def test_a_pause_opens_an_interval_and_a_resume_closes_it():
    paused = p.pause((), at(0))
    assert p.is_paused(paused) and paused == (p.Interval(at(0)),)
    resumed = p.resume(paused, at(30))
    assert not p.is_paused(resumed) and resumed == (p.Interval(at(0), at(30)),)
    assert p.resumed_at(resumed) == at(30)


def test_pausing_a_paused_run_and_resuming_a_running_one_change_nothing():
    paused = p.pause((), at(0))
    assert p.pause(paused, at(5)) == paused
    assert p.resume((), at(5)) == ()
    assert p.resumed_at(()) is None and not p.is_paused(())


def test_an_interval_never_starts_before_the_last_one_ended():
    """Clocks of two console calls may disagree; intervals stay in order and apart."""
    first = p.resume(p.pause((), at(0)), at(30))
    assert p.pause(first, at(20))[-1].start == at(30)
    assert p.resume(p.pause((), at(10)), at(5))[-1].end == at(10)


def test_a_pause_past_ten_merges_the_two_oldest_which_only_moves_deadlines_later():
    intervals = ()
    for k in range(10):
        intervals = p.resume(p.pause(intervals, at(10 * k)), at(10 * k + 5))
    before = p.paused_within(intervals, at(0), at(200))
    merged = p.pause(intervals, at(150))
    assert len(merged) == p.MAX_INTERVALS
    assert merged[0] == p.Interval(at(0), at(15))                 # [0, 5] and [10, 15] became one
    assert p.paused_within(merged, at(0), at(149)) >= before


@pytest.mark.parametrize("start,end,paused", [
    (0, 60, 30),          # the whole interval [10, 40] inside the wait
    (20, 60, 20),         # the wait began during the pause
    (0, 25, 15),          # the wait ended during it
    (50, 60, 0),          # after it
])
def test_the_paused_time_within_a_wait(start, end, paused):
    intervals = p.resume(p.pause((), at(10)), at(40))
    assert p.paused_within(intervals, at(start), at(end)) == timedelta(minutes=paused)


def test_an_open_pause_counts_until_the_end_of_the_window():
    assert p.paused_within(p.pause((), at(10)), at(0), at(25)) == timedelta(minutes=15)


def test_the_row_keeps_epoch_seconds_and_reads_back_what_dynamodb_returns():
    intervals = p.pause(p.resume(p.pause((), at(0)), at(30)), at(45))
    item = p.to_item(intervals)
    assert item == [[int(at(0).timestamp()), int(at(30).timestamp())], [int(at(45).timestamp())]]
    assert p.from_item([[Decimal(n) for n in pair] for pair in item]) == intervals
    assert p.from_item(None) == ()


@pytest.mark.parametrize("value,problem", [
    ("[]", "must be a list"),
    ([[1, 2, 3]], r"must be \[start\] or \[start, end\]"),
    ([["2026-11-18"]], "must be epoch seconds"),
    ([[True]], "must be epoch seconds"),
    ([[1], [2, 3]], "only the latest pause interval may be open"),
])
def test_a_row_with_another_shape_is_refused(value, problem):
    with pytest.raises(ValueError, match=problem):
        p.from_item(value)


def test_naive_times_are_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        p.to_item([p.Interval(datetime(2026, 11, 18))])
