"""Tests for polyris.granularity.infer_cron_cadence.

Covers all cases in ADR #52 "edge cases" matrix. Best-effort inference —
recognized patterns return granularity, ambiguous ones return None.
"""

import pytest

from polyris.constants import SCHEDULE_PRESETS
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
        # */6 in hours = every 6 hours (sub-daily), NOT hourly — would over-expand backfill
        assert infer_cron_cadence("0 */6 * * *") is None

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
        # rate(6 hours) fires every 6 hours — sub-daily, same logic as */6 in hours → None
        assert infer_cron_cadence("rate(6 hours)") is None

    def test_rate_7_days_weekly(self):
        # polyris @weekly preset emits rate(7 days) via SCHEDULE_PRESETS
        assert infer_cron_cadence("rate(7 days)") == "weekly"

    def test_rate_14_days_ambiguous(self):
        assert infer_cron_cadence("rate(14 days)") is None


class TestAWSEventBridgeCron:
    """AWS EventBridge cron(...) wrapper format (6-field with Year)."""

    def test_cron_wrapper_monthly(self):
        assert infer_cron_cadence("cron(0 0 1 * ? *)") == "monthly"

    def test_cron_wrapper_yearly_month_pinned(self):
        # month=1 is non-wildcard → not a recurring four-bucket pattern
        assert infer_cron_cadence("cron(0 0 1 1 ? *)") is None

    def test_daily_eventbridge(self):
        assert infer_cron_cadence("cron(0 8 * * ? *)") == "daily"

    def test_weekly_eventbridge(self):
        assert infer_cron_cadence("cron(0 8 * * MON *)") == "weekly"

    def test_question_mark_in_day_month_position(self):
        # ? in day-of-month (EventBridge style when day-of-week is specified)
        assert infer_cron_cadence("cron(0 8 ? * MON *)") == "weekly"

    def test_pinned_year_ambiguous(self):
        # Year = 2025 means a one-off, not a recurring schedule
        assert infer_cron_cadence("cron(0 8 1 1 ? 2025)") is None

    def test_sub_daily_step_eventbridge(self):
        assert infer_cron_cadence("cron(0 */6 * * ? *)") is None

    def test_six_field_eventbridge_no_wrapper(self):
        # bare 6-field treated as EventBridge format (Year = 6th field = *)
        assert infer_cron_cadence("0 0 8 * * *") == "monthly"


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

    def test_garbage_input(self):
        assert infer_cron_cadence("this is not cron") is None


class TestSchedulePresetsParity:
    """Every SCHEDULE_PRESETS value must be correctly parsed by infer_cron_cadence.

    Imports directly from constants so a change to SCHEDULE_PRESETS breaks here,
    not silently (Principle #28).
    """

    def test_hourly_preset(self):
        assert infer_cron_cadence(SCHEDULE_PRESETS["@hourly"]) == "hourly"

    def test_daily_preset(self):
        assert infer_cron_cadence(SCHEDULE_PRESETS["@daily"]) == "daily"

    def test_weekly_preset(self):
        assert infer_cron_cadence(SCHEDULE_PRESETS["@weekly"]) == "weekly"

    def test_monthly_preset(self):
        assert infer_cron_cadence(SCHEDULE_PRESETS["@monthly"]) == "monthly"

    def test_yearly_preset(self):
        # @yearly fires once a year — not in our four granularities
        assert infer_cron_cadence(SCHEDULE_PRESETS["@yearly"]) is None

    def test_annually_preset(self):
        # alias for @yearly
        assert infer_cron_cadence(SCHEDULE_PRESETS["@annually"]) is None


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
