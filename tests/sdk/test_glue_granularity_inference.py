"""Tests for Glue → granularity inference (ADR #50).

`infer_granularity_from_partition_keys` is advisory-only and pure (no boto3).
"""
import pytest

from polyris.adapters.glue import infer_granularity_from_partition_keys as infer


class TestRecognizedConventions:
    @pytest.mark.parametrize("keys,expected", [
        (["year", "month", "day", "hour"], "hourly"),
        (["year", "month", "day"],         "daily"),
        (["year", "week"],                 "weekly"),
        (["year", "month"],                "monthly"),
    ])
    def test_canonical_patterns(self, keys, expected):
        assert infer(keys) == expected

    @pytest.mark.parametrize("keys,expected", [
        (["Year", "Month", "Day"],         "daily"),
        (["YEAR", "MONTH", "DAY"],         "daily"),
        (["yyyy", "mm", "dd"],             "daily"),
        (["yy", "m", "d"],                 "daily"),
        (["year ", " month", " day "],     "daily"),
    ])
    def test_aliases_and_case(self, keys, expected):
        assert infer(keys) == expected


class TestExtraDimensions:
    def test_non_time_dimension_does_not_block_inference(self):
        assert infer(["region", "year", "month", "day"]) == "daily"

    def test_two_non_time_dimensions_no_inference(self):
        assert infer(["region", "customer_id"]) is None

    def test_empty_keys_no_inference(self):
        assert infer([]) is None


class TestAmbiguousCases:
    def test_hour_alone_is_ambiguous(self):
        assert infer(["hour"]) is None

    def test_day_alone_no_inference(self):
        assert infer(["day"]) is None

    def test_unknown_keys_no_inference(self):
        assert infer(["batch_id", "shard"]) is None

    def test_partial_match_no_inference(self):
        assert infer(["month"]) is None
