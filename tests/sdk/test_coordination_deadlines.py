"""A task row's deadlines and due time, a test per clause (ADR #137 §5, §9, invariant 9; Principle #48)."""

from datetime import datetime, timedelta, timezone

import pytest

from polyris.coordination import attention, pauses
from polyris.coordination import deadlines as d

T0 = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


def at(hours):
    return T0 + timedelta(hours=hours)


def paused(start, end=None):
    """One pause interval, in hours from T0; open when ``end`` is None."""
    intervals = pauses.pause((), at(start))
    return intervals if end is None else pauses.resume(intervals, at(end))


def waiting(**fields):
    return d.RowTimes(status="waiting", waiting_since=T0, **fields)


# ── waiting rows ─────────────────────────────────────────────────────

def test_a_row_that_waits_only_for_other_rows_has_no_time_of_its_own():
    assert d.due_time(waiting(), ()) is None


def test_orchestration_timeout_caps_the_whole_wait_from_its_start():
    assert d.due_time(waiting(orchestration_timeout=2 * HOUR), ()) == at(2)


def test_a_restart_starts_the_wait_again():
    restarted = d.RowTimes(status="waiting", waiting_since=at(10), orchestration_timeout=2 * HOUR)
    assert d.due_time(restarted, ()) == at(12)


def test_wait_for_lasts_seven_days_from_when_the_dependencies_allowed_the_task():
    row = waiting(has_wait_for=True, enabled_at=at(3))
    assert d.due_time(row, ()) == at(3) + d.WAIT_FOR_LIMIT


def test_wait_for_has_no_deadline_while_its_dependencies_still_wait():
    assert d.due_time(waiting(has_wait_for=True), ()) is None


def test_the_earlier_limit_wins():
    row = waiting(has_wait_for=True, enabled_at=at(3), orchestration_timeout=24 * HOUR)
    assert d.due_time(row, ()) == at(24)


def test_paused_time_inside_the_wait_moves_its_deadline():
    assert d.due_time(waiting(orchestration_timeout=2 * HOUR), paused(1, 4)) == at(5)


def test_a_pause_before_the_wait_began_moves_nothing_and_one_overlapping_its_start_moves_the_rest():
    late = d.RowTimes(status="waiting", waiting_since=at(5), orchestration_timeout=2 * HOUR)
    assert d.due_time(late, paused(1, 3)) == at(7)
    assert d.due_time(late, paused(4, 6)) == at(8)


def test_a_pause_after_the_deadline_moves_nothing():
    assert d.due_time(waiting(orchestration_timeout=2 * HOUR), paused(3, 9)) == at(2)


def test_in_a_pause_a_limit_has_no_due_time():
    assert d.due_time(waiting(orchestration_timeout=2 * HOUR), paused(1)) is None


def test_a_deadline_passed_before_the_pause_began_still_falls_due():
    assert d.due_time(waiting(orchestration_timeout=2 * HOUR), paused(3)) == at(2)


def test_a_countdown_ends_where_no_pause_moves_it_and_no_limit_applies():
    end = d.countdown_end(at(1), timedelta(minutes=30))
    row = waiting(orchestration_timeout=HOUR, countdown_end=end)
    assert d.due_time(row, paused(1, 5)) == at(1) + timedelta(minutes=30)
    assert d.wait_deadline(row, ()) is None
    assert d.countdown_end(at(1), timedelta(0)) is None


# ── a start that keeps failing ───────────────────────────────────────

def failing(failures, last_minutes):
    return d.RowTimes(status="deps_ready", status_since=T0, start_failures=failures, start_failed_first=T0,
                      start_failed_last=T0 + timedelta(minutes=last_minutes))


@pytest.mark.parametrize("failures,backoff", [(1, 30), (2, 60), (3, 120), (4, 300), (9, 300)])
def test_a_failed_start_is_tried_again_at_most_five_minutes_later(failures, backoff):
    assert d.due_time(failing(failures, 10), ()) == T0 + timedelta(minutes=10, seconds=backoff)


def test_a_failing_start_warns_at_15_minutes_and_fails_at_6_hours_from_its_first_failure():
    start = d.start_deadlines(failing(3, 10), ())
    assert (start.warn_at, start.fail_at) == (T0 + d.START_WARN_AFTER, T0 + d.START_FAIL_AFTER)


def test_paused_time_moves_both_and_a_pause_holds_the_start():
    start = d.start_deadlines(failing(3, 10), paused(1, 3))
    assert (start.warn_at, start.fail_at) == (T0 + d.START_WARN_AFTER, T0 + d.START_FAIL_AFTER + 2 * HOUR)
    assert d.due_time(failing(3, 10), paused(1)) is None


def test_a_start_with_no_failed_attempt_has_no_start_deadlines():
    assert d.start_deadlines(d.RowTimes(status="deps_ready", status_since=T0), ()) is None


# ── liveness checks and decisions ────────────────────────────────────

@pytest.mark.parametrize("status", ["deps_ready", "running"])
def test_a_started_row_is_checked_on_the_attention_backoff(status):
    row = d.RowTimes(status=status, status_since=T0, checks=2)
    assert d.due_time(row, ()) == attention.next_due(T0, 2)
    assert d.due_time(d.RowTimes(status=status), ()) is None


def test_a_decision_deadline_is_the_timeout_in_force_moved_by_the_paused_time():
    row = d.RowTimes(status="waiting_decision", decision_since=T0, decision_timeout=5 * HOUR)
    assert d.due_time(row, ()) == at(5)
    assert d.due_time(row, paused(2, 4)) == at(7)


def test_in_a_pause_a_decision_deadline_falls_due_far_in_the_future():
    row = d.RowTimes(status="waiting_decision", decision_since=T0, decision_timeout=5 * HOUR)
    assert d.due_time(row, paused(2)) == d.FAR_FUTURE
    assert d.decision_deadline(d.RowTimes(status="waiting_decision"), ()) is None


@pytest.mark.parametrize("status", ["waiting_paused", "success", "failed", "skipped", "upstream_failed", "stopped"])
def test_rows_a_pause_holds_and_final_rows_have_no_due_time(status):
    assert d.due_time(d.RowTimes(status=status, waiting_since=T0, orchestration_timeout=HOUR), ()) is None
