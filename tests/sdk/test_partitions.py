"""Tests for polyris.partitions.PartitionRange.

Covers ADR #58 edge cases table and translation rules per ADR #57.
Pure stdlib, no AWS calls — fast.
"""

import pytest

from polyris.partitions import PartitionRange


# ===========================================================================
# Construction validation
# ===========================================================================

class TestConstruction:
    def test_empty_range_valid(self):
        pr = PartitionRange(keys=[], granularity="daily")
        assert len(pr) == 0

    def test_unknown_granularity_rejected(self):
        with pytest.raises(ValueError, match="Unknown granularity"):
            PartitionRange(keys=[], granularity="quarterly")  # type: ignore

    def test_key_format_validated_daily(self):
        with pytest.raises(ValueError, match="does not match"):
            PartitionRange(keys=["2024-01"], granularity="daily")  # wrong format

    def test_key_format_validated_weekly(self):
        with pytest.raises(ValueError, match="does not match"):
            PartitionRange(keys=["2024-01-15"], granularity="weekly")

    def test_key_format_validated_monthly(self):
        with pytest.raises(ValueError, match="does not match"):
            PartitionRange(keys=["2024-01-15"], granularity="monthly")

    def test_key_format_validated_hourly(self):
        with pytest.raises(ValueError, match="does not match"):
            PartitionRange(keys=["2024-01-15"], granularity="hourly")

    def test_iteration(self):
        pr = PartitionRange(keys=["2024-01-15", "2024-01-16"], granularity="daily")
        assert list(pr) == ["2024-01-15", "2024-01-16"]

    def test_contains(self):
        pr = PartitionRange(keys=["2024-01-15"], granularity="daily")
        assert "2024-01-15" in pr
        assert "2024-01-16" not in pr


# ===========================================================================
# expand() — range to keys
# ===========================================================================

class TestExpandDaily:
    def test_single_day(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-15", "daily")
        assert pr.keys == ["2024-01-15"]

    def test_three_days(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-17", "daily")
        assert pr.keys == ["2024-01-15", "2024-01-16", "2024-01-17"]

    def test_month_boundary(self):
        pr = PartitionRange.expand("2024-01-30", "2024-02-02", "daily")
        assert pr.keys == ["2024-01-30", "2024-01-31", "2024-02-01", "2024-02-02"]

    def test_year_boundary(self):
        pr = PartitionRange.expand("2023-12-30", "2024-01-02", "daily")
        assert pr.keys == ["2023-12-30", "2023-12-31", "2024-01-01", "2024-01-02"]

    def test_leap_year(self):
        pr = PartitionRange.expand("2024-02-28", "2024-03-01", "daily")
        assert pr.keys == ["2024-02-28", "2024-02-29", "2024-03-01"]

    def test_non_leap_year(self):
        pr = PartitionRange.expand("2023-02-28", "2023-03-01", "daily")
        assert pr.keys == ["2023-02-28", "2023-03-01"]


class TestExpandWeekly:
    def test_single_week(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-15", "weekly")
        # Jan 15 2024 is a Monday — ISO week 3
        assert pr.keys == ["2024-W03"]

    def test_three_weeks(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-31", "weekly")
        assert pr.keys == ["2024-W03", "2024-W04", "2024-W05"]

    def test_mid_week_inputs_clip_to_monday(self):
        # Wednesday Jan 17 → ISO week 3 starts Monday Jan 15
        pr = PartitionRange.expand("2024-01-17", "2024-01-17", "weekly")
        assert pr.keys == ["2024-W03"]

    def test_iso_year_boundary(self):
        # 2024-12-30 (Monday) is ISO week 2025-W01 (yes, surprising)
        pr = PartitionRange.expand("2024-12-30", "2024-12-30", "weekly")
        assert pr.keys == ["2025-W01"]

    def test_iso_week_format_input(self):
        pr = PartitionRange.expand("2024-W03", "2024-W05", "weekly")
        assert pr.keys == ["2024-W03", "2024-W04", "2024-W05"]


class TestExpandMonthly:
    def test_single_month(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-31", "monthly")
        assert pr.keys == ["2024-01"]

    def test_three_months(self):
        pr = PartitionRange.expand("2024-01-15", "2024-03-15", "monthly")
        assert pr.keys == ["2024-01", "2024-02", "2024-03"]

    def test_december_to_january_crosses_year(self):
        pr = PartitionRange.expand("2024-12-01", "2025-02-01", "monthly")
        assert pr.keys == ["2024-12", "2025-01", "2025-02"]

    def test_monthly_format_input(self):
        pr = PartitionRange.expand("2024-01", "2024-03", "monthly")
        assert pr.keys == ["2024-01", "2024-02", "2024-03"]


class TestExpandHourly:
    def test_single_hour(self):
        pr = PartitionRange.expand("2024-01-15T14", "2024-01-15T14", "hourly")
        assert pr.keys == ["2024-01-15T14"]

    def test_three_hours(self):
        pr = PartitionRange.expand("2024-01-15T14", "2024-01-15T16", "hourly")
        assert pr.keys == ["2024-01-15T14", "2024-01-15T15", "2024-01-15T16"]

    def test_day_boundary(self):
        pr = PartitionRange.expand("2024-01-15T23", "2024-01-16T01", "hourly")
        assert pr.keys == ["2024-01-15T23", "2024-01-16T00", "2024-01-16T01"]

    def test_date_only_input_defaults_to_hour_0(self):
        pr = PartitionRange.expand("2024-01-15", "2024-01-15", "hourly")
        # Single hour (midnight)
        assert pr.keys == ["2024-01-15T00"]

    def test_full_day_24_hours(self):
        pr = PartitionRange.expand("2024-01-15T00", "2024-01-15T23", "hourly")
        assert len(pr) == 24


# ===========================================================================
# partition_start clipping
# ===========================================================================

class TestPartitionStartClipping:
    def test_partition_start_before_range_no_effect(self):
        pr = PartitionRange.expand(
            "2024-01-15", "2024-01-17", "daily",
            partition_start="2024-01-01",
        )
        assert pr.keys == ["2024-01-15", "2024-01-16", "2024-01-17"]

    def test_partition_start_clips(self):
        pr = PartitionRange.expand(
            "2024-01-01", "2024-01-05", "daily",
            partition_start="2024-01-03",
        )
        assert pr.keys == ["2024-01-03", "2024-01-04", "2024-01-05"]

    def test_partition_start_clips_weekly(self):
        # partition_start in week 2024-W03 clips
        pr = PartitionRange.expand(
            "2024-W01", "2024-W05", "weekly",
            partition_start="2024-W03",
        )
        assert pr.keys == ["2024-W03", "2024-W04", "2024-W05"]

    def test_partition_start_clips_monthly(self):
        pr = PartitionRange.expand(
            "2024-01", "2024-06", "monthly",
            partition_start="2024-04",
        )
        assert pr.keys == ["2024-04", "2024-05", "2024-06"]


# ===========================================================================
# Error paths
# ===========================================================================

class TestExpandErrors:
    def test_reverse_range_rejected(self):
        with pytest.raises(ValueError, match="before start"):
            PartitionRange.expand("2024-01-17", "2024-01-15", "daily")

    def test_hard_limit_exceeded(self):
        # 6000 days exceeds 1000 limit
        with pytest.raises(ValueError, match="hard limit"):
            PartitionRange.expand("2000-01-01", "2030-01-01", "daily")

    def test_unknown_granularity(self):
        with pytest.raises(ValueError, match="Unknown granularity"):
            PartitionRange.expand("2024-01-01", "2024-01-15", "quarterly")  # type: ignore

    def test_malformed_input_raises(self):
        with pytest.raises(ValueError):
            PartitionRange.expand("not-a-date", "2024-01-15", "daily")


# ===========================================================================
# from_keys()
# ===========================================================================

class TestFromKeys:
    def test_explicit_list(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15", "2024-01-22", "2024-02-01"], "daily",
        )
        assert pr.keys == ["2024-01-15", "2024-01-22", "2024-02-01"]

    def test_sorted_on_construction(self):
        pr = PartitionRange.from_keys(
            ["2024-02-01", "2024-01-15", "2024-01-22"], "daily",
        )
        assert pr.keys == ["2024-01-15", "2024-01-22", "2024-02-01"]

    def test_deduped(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15", "2024-01-15", "2024-01-22"], "daily",
        )
        assert pr.keys == ["2024-01-15", "2024-01-22"]

    def test_invalid_key_format_rejected(self):
        with pytest.raises(ValueError, match="does not match"):
            PartitionRange.from_keys(["2024-01"], "daily")

    def test_hard_limit_enforced(self):
        from datetime import date, timedelta
        # Generate >1000 unique daily keys
        start = date(2010, 1, 1)
        keys = [(start + timedelta(days=i)).isoformat() for i in range(5500)]
        with pytest.raises(ValueError, match="hard limit"):
            PartitionRange.from_keys(keys, "daily")


# ===========================================================================
# translate_to() — granularity transformation
# ===========================================================================

class TestTranslate:
    def test_same_granularity_returns_copy(self):
        pr = PartitionRange.from_keys(["2024-01-15"], "daily")
        translated = pr.translate_to("daily")
        assert translated.keys == pr.keys
        assert translated is not pr  # copy, not same object

    def test_daily_to_weekly(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15", "2024-01-16", "2024-01-22"], "daily",
        )
        # Jan 15-16 = week W03, Jan 22 = week W04
        translated = pr.translate_to("weekly")
        assert translated.keys == ["2024-W03", "2024-W04"]
        assert translated.granularity == "weekly"

    def test_daily_to_monthly(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15", "2024-01-22", "2024-02-01"], "daily",
        )
        translated = pr.translate_to("monthly")
        assert translated.keys == ["2024-01", "2024-02"]

    def test_hourly_to_daily(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15T08", "2024-01-15T14", "2024-01-16T03"], "hourly",
        )
        translated = pr.translate_to("daily")
        assert translated.keys == ["2024-01-15", "2024-01-16"]

    def test_weekly_to_monthly(self):
        pr = PartitionRange.from_keys(["2024-W03", "2024-W04"], "weekly")
        # Both weeks are in January
        translated = pr.translate_to("monthly")
        assert translated.keys == ["2024-01"]

    def test_finer_target_rejected_monthly_to_daily(self):
        # Coarsening-only: re-bucketing to a FINER granularity would silently
        # drop partitions (a month floors to a single day), so it must raise
        # rather than quietly lose data (audit #5d).
        pr = PartitionRange.from_keys(["2024-01", "2024-02"], "monthly")
        with pytest.raises(ValueError, match="only coarsens"):
            pr.translate_to("daily")

    def test_finer_target_rejected_daily_to_hourly(self):
        pr = PartitionRange.from_keys(["2024-01-15"], "daily")
        with pytest.raises(ValueError, match="only coarsens"):
            pr.translate_to("hourly")

    def test_unknown_target_granularity(self):
        pr = PartitionRange.from_keys(["2024-01-15"], "daily")
        with pytest.raises(ValueError, match="Unknown granularity"):
            pr.translate_to("yearly")  # type: ignore


# ===========================================================================
# skip_completed()
# ===========================================================================

class TestSkipCompleted:
    def test_skip_subset(self):
        pr = PartitionRange.from_keys(
            ["2024-01-15", "2024-01-16", "2024-01-17"], "daily",
        )
        remaining = pr.skip_completed({"2024-01-15", "2024-01-17"})
        assert remaining.keys == ["2024-01-16"]

    def test_skip_none(self):
        pr = PartitionRange.from_keys(["2024-01-15"], "daily")
        remaining = pr.skip_completed(set())
        assert remaining.keys == ["2024-01-15"]

    def test_skip_all(self):
        pr = PartitionRange.from_keys(["2024-01-15", "2024-01-16"], "daily")
        remaining = pr.skip_completed({"2024-01-15", "2024-01-16"})
        assert remaining.keys == []

    def test_skip_keys_not_in_range_ignored(self):
        pr = PartitionRange.from_keys(["2024-01-15"], "daily")
        remaining = pr.skip_completed({"2024-12-31"})  # not in range
        assert remaining.keys == ["2024-01-15"]


# ===========================================================================
# Cost estimate tests removed v0.78.2 — see ADR #62. cost_estimate()
# method was removed; the class no longer exists. Its tests are deleted
# rather than skipped per CLAUDE.md #11. Pro-tier cost reporting will
# bring its own (more comprehensive) test suite when reintroduced.
# ===========================================================================



# ===========================================================================
# Lex sortability of partition keys (used by from_keys)
# ===========================================================================

class TestKeyLexSort:
    """All four formats sort correctly when compared as strings."""

    def test_daily_lex_sort(self):
        keys = ["2024-02-01", "2024-01-15", "2024-01-30"]
        assert sorted(keys) == ["2024-01-15", "2024-01-30", "2024-02-01"]

    def test_weekly_lex_sort(self):
        keys = ["2024-W10", "2024-W02", "2024-W09"]
        # ISO 8601 zero-padded weeks sort correctly
        assert sorted(keys) == ["2024-W02", "2024-W09", "2024-W10"]

    def test_monthly_lex_sort(self):
        keys = ["2024-12", "2024-01", "2024-05"]
        assert sorted(keys) == ["2024-01", "2024-05", "2024-12"]

    def test_hourly_lex_sort(self):
        keys = ["2024-01-15T23", "2024-01-15T00", "2024-01-15T13"]
        assert sorted(keys) == [
            "2024-01-15T00", "2024-01-15T13", "2024-01-15T23",
        ]
