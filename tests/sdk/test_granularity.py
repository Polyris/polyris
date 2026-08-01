"""Tests for polyris.granularity.infer_cron_cadence.

Covers all cases in ADR #52 "edge cases" matrix. Best-effort inference —
recognized patterns return granularity, ambiguous ones return None.
"""

import pytest

from polyris.granularity import infer_cron_cadence


class TestStandardCron:
    """Standard 5-field cron expressions."""

    def test_daily_fixed_hour(self):
        assert infer_cron_cadence("0 8 * * *") == "daily"

    def test_daily_midnight(self):
        assert infer_cron_cadence("30 0 * * *") == "daily"

    def test_daily_late(self):
        assert infer_cron_cadence("0 23 * * *") == "daily"

    def test_hourly_wildcard(self):
        assert infer_cron_cadence("0 * * * *") == "hourly"

    def test_hourly_step(self):
        assert infer_cron_cadence("0 */6 * * *") == "hourly"

    def test_hourly_every_15min(self):
        # Sub-hourly buckets as hourly
        assert infer_cron_cadence("*/15 * * * *") == "hourly"

    def test_weekly_named_monday(self):
        assert infer_cron_cadence("0 8 * * MON") == "weekly"

    def test_weekly_numeric(self):
        assert infer_cron_cadence("0 8 * * 1") == "weekly"

    def test_weekly_sunday(self):
        assert infer_cron_cadence("0 8 * * SUN") == "weekly"

    def test_weekly_friday(self):
        assert infer_cron_cadence("0 8 * * FRI") == "weekly"

    def test_monthly_first_day(self):
        assert infer_cron_cadence("0 8 1 * *") == "monthly"

    def test_monthly_mid_month(self):
        assert infer_cron_cadence("0 8 15 * *") == "monthly"

    def test_monthly_last_day_marker_not_supported(self):
        # "L" syntax (last day of month) is non-standard; we don't infer
        assert infer_cron_cadence("0 8 L * *") is None


class TestAmbiguousPatterns:
    """Patterns that don't fit one of our four buckets."""

    def test_weekdays_only(self):
        # 5-day-a-week is neither pure daily nor pure weekly
        assert infer_cron_cadence("0 8 * * 1-5") is None

    def test_multiple_days_of_week(self):
        assert infer_cron_cadence("0 8 * * MON,WED,FRI") is None

    def test_twice_monthly(self):
        assert infer_cron_cadence("0 8 1,15 * *") is None

    def test_quarterly(self):
        # Once per quarter via month list — month field is non-wildcard, rejected
        assert infer_cron_cadence("0 8 1 1,4,7,10 *") is None

    def test_business_hours_frequency(self):
        # Highly irregular: multiple wildcards combined
        assert infer_cron_cadence("*/5 9-17 * * 1-5") is None

    def test_hour_range(self):
        # Multiple hours per day → ambiguous
        assert infer_cron_cadence("0 9-17 * * *") is None

    def test_nth_weekday_of_month(self):
        # First Monday of month: month=* but day_week uses '#'; rejected
        assert infer_cron_cadence("0 0 ? * MON#1") is None

    def test_yearly(self):
        # Specific month → not in our set
        assert infer_cron_cadence("0 8 1 1 *") is None


class TestShorthand:
    """@shorthand patterns."""

    def test_at_daily(self):
        assert infer_cron_cadence("@daily") == "daily"

    def test_at_weekly(self):
        assert infer_cron_cadence("@weekly") == "weekly"

    def test_at_monthly(self):
        assert infer_cron_cadence("@monthly") == "monthly"

    def test_at_hourly(self):
        assert infer_cron_cadence("@hourly") == "hourly"

    def test_at_midnight(self):
        # Synonym for daily
        assert infer_cron_cadence("@midnight") == "daily"

    def test_at_yearly_not_supported(self):
        # No yearly granularity in our model
        assert infer_cron_cadence("@yearly") is None


class TestAWSRate:
    """AWS EventBridge rate(N unit) expressions."""

    def test_rate_1_hour(self):
        assert infer_cron_cadence("rate(1 hour)") == "hourly"

    def test_rate_1_day(self):
        assert infer_cron_cadence("rate(1 day)") == "daily"

    def test_rate_5_minutes(self):
        # Sub-hourly buckets as hourly
        assert infer_cron_cadence("rate(5 minutes)") == "hourly"

    def test_rate_2_days_ambiguous(self):
        # We don't have a "biday" granularity
        assert infer_cron_cadence("rate(2 days)") is None

    def test_rate_multi_hour(self):
        # Multiple hours = still hourly-bucketed cadence
        assert infer_cron_cadence("rate(6 hours)") == "hourly"


class TestEmptyAndInvalid:
    """Edge cases on input shape."""

    def test_none_input(self):
        # Manual-only pipeline (no schedule) defaults to daily
        assert infer_cron_cadence(None) == "daily"

    def test_empty_string(self):
        assert infer_cron_cadence("") == "daily"

    def test_whitespace_only(self):
        assert infer_cron_cadence("   ") == "daily"

    def test_malformed_too_few_fields(self):
        assert infer_cron_cadence("0 8 *") is None

    def test_malformed_too_many_fields(self):
        # 6-field cron (with seconds) is not supported
        assert infer_cron_cadence("0 0 8 * * *") is None

    def test_garbage_input(self):
        assert infer_cron_cadence("this is not cron") is None


class TestNoExceptions:
    """Ensure inference never crashes regardless of input."""

    @pytest.mark.parametrize(
        "bad",
        [
            "%%%%%",
            "0 0 0 0 0",  # all zeros — technically valid cron, monthly-like
            "* * * * * *",  # 6 stars
            "0 8 * * MON,",  # trailing comma
            "// // // // //",
            "rate(invalid)",
            "rate(-1 hour)",  # negative
            "@unknown",
        ],
    )
    def test_does_not_raise(self, bad):
        # We expect either a valid granularity or None; never an exception
        result = infer_cron_cadence(bad)
        assert result is None or result in ("hourly", "daily", "weekly", "monthly")
