"""Partition keys on one date line (ADR #130 §1, §4).

Covers ``polyris.partitions.detect_granularity`` / ``partition_start`` and
``polyris.coordination.partition``.
"""

from datetime import date, datetime, timezone

import pytest

from polyris.coordination.partition import (
    EXACT_GRANULARITIES,
    PERIOD_GRANULARITIES,
    in_current_or_previous_period,
    oldest_useful_date,
    partition_date,
)
from polyris.partitions import detect_granularity, partition_start


class TestDetectGranularity:
    @pytest.mark.parametrize("key,expected", [
        ("2024-01-15T14", "hourly"),
        ("2024-01-15", "daily"),
        ("2024-W03", "weekly"),
        ("2024-01", "monthly"),
    ])
    def test_each_canonical_format(self, key, expected):
        assert detect_granularity(key) == expected

    @pytest.mark.parametrize("key", ["", "2024/01/15", "2024-1-5", "2024-13", "2024-W99", "2024-02-31", "W03"])
    def test_rejects_non_keys_and_impossible_dates(self, key):
        with pytest.raises(ValueError):
            detect_granularity(key)


class TestPartitionStart:
    @pytest.mark.parametrize("key,expected", [
        ("2024-01-15T14", datetime(2024, 1, 15, 14, tzinfo=timezone.utc)),
        ("2024-01-15T14Z", datetime(2024, 1, 15, 14, tzinfo=timezone.utc)),
        ("2024-01-15", datetime(2024, 1, 15, tzinfo=timezone.utc)),
        # ISO week 3 of 2024 starts on Monday 15 January.
        ("2024-W03", datetime(2024, 1, 15, tzinfo=timezone.utc)),
        ("2024-01", datetime(2024, 1, 1, tzinfo=timezone.utc)),
    ])
    def test_start_of_the_named_period(self, key, expected):
        assert partition_start(key) == expected

    @pytest.mark.parametrize("key,expected", [
        ("2024-01-15T14", date(2024, 1, 15)),
        ("2024-01-15", date(2024, 1, 15)),
        ("2024-W03", date(2024, 1, 15)),
        ("2024-01", date(2024, 1, 1)),
    ])
    def test_partition_date_is_the_start_date(self, key, expected):
        assert partition_date(key) == expected


class TestCalendarPeriods:
    # 2026-11-11 is a Wednesday in ISO week 46 (Monday 2026-11-09).
    @pytest.mark.parametrize("fact,expected", [
        (date(2026, 11, 9), True),    # Monday of the same week
        (date(2026, 11, 13), True),   # later in the same week (callers also require fact <= target)
        (date(2026, 11, 8), True),    # Sunday of the previous week
        (date(2026, 11, 2), True),    # Monday of the previous week
        (date(2026, 11, 1), False),   # two weeks back
        (date(2026, 11, 16), False),  # next week
    ])
    def test_weekly_is_this_or_previous_iso_week(self, fact, expected):
        assert in_current_or_previous_period(fact, date(2026, 11, 11), "weekly") is expected

    @pytest.mark.parametrize("fact,target,expected", [
        (date(2026, 3, 2), date(2026, 4, 3), True),     # first Monday of March, early April
        (date(2026, 4, 1), date(2026, 4, 3), True),
        (date(2026, 2, 28), date(2026, 4, 3), False),   # two months back
        (date(2025, 12, 31), date(2026, 1, 15), True),  # across the year boundary
        (date(2025, 11, 30), date(2026, 1, 15), False),
    ])
    def test_monthly_is_this_or_previous_calendar_month(self, fact, target, expected):
        assert in_current_or_previous_period(fact, target, "monthly") is expected

    @pytest.mark.parametrize("granularity", ["daily", "hourly", "yearly"])
    def test_granularities_without_a_period_raise(self, granularity):
        with pytest.raises(ValueError):
            in_current_or_previous_period(date(2026, 1, 1), date(2026, 1, 2), granularity)

    def test_granularity_sets_are_disjoint_and_complete(self):
        assert not EXACT_GRANULARITIES & PERIOD_GRANULARITIES
        assert EXACT_GRANULARITIES | PERIOD_GRANULARITIES == {"hourly", "daily", "weekly", "monthly"}


class TestOldestUsefulDate:
    @pytest.mark.parametrize("target,expected", [
        (date(2026, 3, 15), date(2026, 2, 1)),
        (date(2026, 3, 1), date(2026, 2, 1)),
        (date(2026, 1, 10), date(2025, 12, 1)),
    ])
    def test_first_of_the_previous_month(self, target, expected):
        assert oldest_useful_date(target) == expected
