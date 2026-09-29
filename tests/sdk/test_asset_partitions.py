"""SDK tests for Asset.granularity and Asset.partition_start (ADR #50).

Tests cover:
  - Default behavior (no fields → daily, no start) — backward compat
  - All four supported granularities (hourly, daily, weekly, monthly)
  - Validation: bad granularity name caught at construction
  - Validation: partition_start format must match granularity
  - Edge cases: partition_start optional, granularity is sticky
"""
import pytest

from polyris.assets import Asset


class TestDefaults:
    def test_no_partition_fields_defaults_to_daily(self):
        asset = Asset("acme/orders")
        assert asset.granularity == "daily"
        assert asset.partition_start is None

    def test_uri_only_construction_also_daily(self):
        asset = Asset(uri="s3://bucket/acme/orders/")
        assert asset.granularity == "daily"


class TestGranularities:
    @pytest.mark.parametrize("granularity,start", [
        ("hourly",  "2024-01-01T00"),
        ("daily",   "2024-01-01"),
        ("weekly",  "2024-W01"),
        ("monthly", "2024-01"),
    ])
    def test_each_granularity_accepts_matching_start(self, granularity, start):
        asset = Asset(
            "acme/x",
            granularity=granularity,
            partition_start=start,
        )
        assert asset.granularity == granularity
        assert asset.partition_start == start

    def test_partition_start_optional(self):
        asset = Asset("acme/x", granularity="weekly")
        assert asset.granularity == "weekly"
        assert asset.partition_start is None


class TestValidation:
    def test_unknown_granularity_rejected(self):
        with pytest.raises(ValueError, match="granularity must be one of"):
            Asset("x", granularity="yearly")  # type: ignore[arg-type]

    @pytest.mark.parametrize("granularity,bad_start", [
        ("daily",   "2024-01"),
        ("daily",   "Jan 1 2024"),
        ("weekly",  "2024-01-01"),
        ("monthly", "2024-01-15"),
        ("hourly",  "2024-01-01"),
    ])
    def test_partition_start_format_must_match_granularity(self, granularity, bad_start):
        with pytest.raises(ValueError, match="does not match the format"):
            Asset("x", granularity=granularity, partition_start=bad_start)

    @pytest.mark.parametrize("granularity,bad_start", [
        ("daily",   "2024-02-31"),   # Feb has no 31st
        ("daily",   "2023-02-29"),   # 2023 is not a leap year
        ("weekly",  "2024-W99"),     # week 99 does not exist
        ("weekly",  "2024-W53"),     # 2024 has only 52 ISO weeks
        ("weekly",  "2024-W00"),     # week 0 does not exist
        ("monthly", "2024-13"),      # month 13 does not exist
        ("hourly",  "2024-02-31T10"), # Feb has no 31st
    ])
    def test_partition_start_calendar_validity(self, granularity, bad_start):
        with pytest.raises(ValueError, match="is not a valid"):
            Asset("x", granularity=granularity, partition_start=bad_start)

    @pytest.mark.parametrize("granularity,good_start", [
        ("daily",  "2024-02-29"),   # 2024 is a leap year
        ("weekly", "2015-W53"),     # 2015 is a long ISO year with 53 weeks
    ])
    def test_partition_start_valid_boundary_accepted(self, granularity, good_start):
        asset = Asset("x", granularity=granularity, partition_start=good_start)
        assert asset.partition_start == good_start

    def test_hourly_accepts_timezone_suffix(self):
        asset = Asset(
            "x",
            granularity="hourly",
            partition_start="2024-01-01T00Z",
        )
        assert asset.partition_start == "2024-01-01T00Z"


class TestInteraction:
    def test_partition_fields_coexist_with_glue_table(self):
        asset = Asset(
            "acme/orders",
            granularity="weekly",
            partition_start="2024-W01",
            glue_table="default.orders",
        )
        assert asset.granularity == "weekly"
        assert asset.glue_table == "default.orders"

    def test_partition_fields_coexist_with_schema(self):
        from polyris.schema import Column, bigint
        asset = Asset(
            "acme/orders",
            granularity="daily",
            schema=[Column("id", bigint())],
        )
        assert asset.granularity == "daily"
        assert len(asset.schema) == 1
