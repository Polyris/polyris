"""How long a run's rows live, and the Restart's margin (ADR #137 §5, D108, D109)."""

from datetime import datetime, timedelta, timezone

import pytest

from polyris.coordination import retention as r

STARTED = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)


def test_the_history_outlives_any_run_and_the_input_lives_the_consoles_window():
    assert r.HISTORY == timedelta(days=455) and r.HISTORY > r.YEAR
    assert r.OUTPUT == r.YEAR == timedelta(days=365)
    assert r.INPUT == r.TIMELINE == r.BACKFILL_ENDED == timedelta(days=90)
    assert r.BACKFILL_RUNNING == r.HISTORY


def test_a_ttl_counts_from_the_write():
    assert r.ttl(STARTED, r.INPUT) == int((STARTED + timedelta(days=90)).timestamp())


@pytest.mark.parametrize("after,allowed", [
    (timedelta(days=270), True),
    (timedelta(days=275) - timedelta(seconds=1), True),
    (timedelta(days=275), False),                          # a year less 90 days (RESTART_TTL_MARGIN, D126)
    (timedelta(days=400), False),
])
def test_a_restart_is_refused_once_its_run_started_a_year_ago_less_90_days(after, allowed):
    assert r.restart_allowed(STARTED, STARTED + after) is allowed


def test_the_last_accepted_restart_still_finds_90_days_of_its_upstreams_outputs():
    """An upstream's output# was written after its run started, so it lives at least until then + a year."""
    last = STARTED + r.YEAR - r.RESTART_TTL_MARGIN - timedelta(seconds=1)
    assert r.restart_allowed(STARTED, last)
    assert r.ttl(STARTED, r.OUTPUT) - last.timestamp() >= r.RESTART_TTL_MARGIN.total_seconds()


@pytest.mark.parametrize("call", [
    lambda: r.ttl(datetime(2026, 11, 18), r.INPUT),
    lambda: r.restart_allowed(datetime(2026, 11, 18), STARTED),
    lambda: r.restart_allowed(STARTED, datetime(2026, 11, 18)),
])
def test_naive_times_are_refused(call):
    with pytest.raises(ValueError, match="timezone-aware"):
        call()
