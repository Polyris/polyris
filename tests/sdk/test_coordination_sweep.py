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


def test_push_retries_back_off_from_5_minutes_to_an_hour():
    delays = [sweep.next_push_retry(STARTED, n) - STARTED for n in range(6)]
    assert delays == [timedelta(minutes=m) for m in (5, 10, 20, 40, 60, 60)]


def test_the_catch_up_runs_hourly_and_at_once_when_it_never_ran():
    assert sweep.catch_up_due(STARTED, None)
    assert sweep.catch_up_due(STARTED, STARTED - timedelta(hours=1))
    assert not sweep.catch_up_due(STARTED, STARTED - timedelta(minutes=59))


def _health(**fields):
    base = {"checked_at": "2026-11-12T04:58:00.000Z", "fanout_mapping": "Enabled", "stream": "ENABLED",
            "oldest_overdue": ""}
    return {**base, **fields}


def test_a_fresh_report_with_everything_enabled_is_healthy():
    assert sweep.engine_problems(_health(), STARTED) == []
    assert sweep.engine_problems(_health(oldest_overdue="2026-11-12T04:30:00.000Z"), STARTED) == []


def test_every_way_the_engine_can_be_unhealthy_is_named():
    assert sweep.engine_problems(None, STARTED) == ["The sweeper has never reported: waiting tasks are not re-checked."]
    problems = sweep.engine_problems(_health(checked_at="2026-11-12T04:40:00.000Z", fanout_mapping="Disabled",
                                             stream="", oldest_overdue="2026-11-12T04:00:00.000Z"), STARTED)
    assert problems == [
        "The sweeper last reported 20 minutes ago: waiting tasks are not re-checked.",
        "Asset events are not delivered: the stream mapping is Disabled.",
        "Asset events are not recorded for delivery: the table stream is missing.",
        "Waiting tasks are 60 minutes behind their checks.",
    ]


def test_the_lease_outlives_a_run_but_not_the_schedule():
    assert sweep.BUDGET < sweep.LEASE < timedelta(minutes=5)
