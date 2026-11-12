"""The sweeper's pure rules (ADR #130 §3, §8)."""

from datetime import datetime, timedelta, timezone

from polyris.coordination import sweep

STARTED = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)


def test_the_liveness_deadline_covers_three_orchestration_timeouts_and_an_hour():
    assert sweep.liveness_deadline(STARTED, 3600) == STARTED + timedelta(hours=4)


def test_a_row_without_an_orchestration_timeout_uses_the_sdk_default():
    assert sweep.liveness_deadline(STARTED, None) == STARTED + timedelta(hours=73)
    assert sweep.liveness_deadline(STARTED, 0) == STARTED + timedelta(hours=73)


def test_a_wake_is_a_primary_path_miss_only_when_the_task_could_run_before_its_due_time():
    due = STARTED + timedelta(minutes=10)
    assert sweep.primary_path_missed(due - timedelta(seconds=1), due)
    assert not sweep.primary_path_missed(due, due)
    assert not sweep.primary_path_missed(None, due)


def test_the_lease_outlives_a_run_but_not_the_schedule():
    assert sweep.BUDGET < sweep.LEASE < timedelta(minutes=5)
