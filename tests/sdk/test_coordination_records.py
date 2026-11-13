"""Timestamps, attention keys and row shapes (ADR #130 §1, §7, §9)."""

import hashlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from polyris.coordination import attention as att
from polyris.coordination.records import (
    FACT_VERSION,
    PARTITION_INDEX,
    PARTITION_INDEX_PROJECTION,
    asset_fact_item,
    fact_from_item,
    push_retry_item,
    warning_item,
)
from polyris.coordination.times import format_utc, parse_utc

NOW = datetime(2026, 11, 12, 6, 0, 0, 123456, tzinfo=timezone.utc)


class TestTimes:
    def test_format_matches_step_functions_now(self):
        assert format_utc(NOW) == "2026-11-12T06:00:00.123Z"

    def test_other_offsets_are_converted_to_utc(self):
        plus_two = NOW.astimezone(timezone(timedelta(hours=2)))
        assert format_utc(plus_two) == "2026-11-12T06:00:00.123Z"

    def test_naive_datetimes_are_rejected(self):
        with pytest.raises(ValueError):
            format_utc(datetime(2026, 11, 12))

    @pytest.mark.parametrize("text", [
        "2026-11-12T06:00:00.123Z",
        "2026-11-12T06:00:00.123000+00:00",   # older Python writers
        "2026-11-12T08:00:00.123+02:00",
    ])
    def test_parse_accepts_every_stored_form(self, text):
        assert parse_utc(text) == NOW.replace(microsecond=123000)

    def test_parse_rejects_naive_text(self):
        with pytest.raises(ValueError):
            parse_utc("2026-11-12T06:00:00")

    def test_string_order_is_time_order(self):
        earlier, later = format_utc(NOW), format_utc(NOW + timedelta(milliseconds=1))
        assert earlier < later


def _template() -> dict:
    """sam/template.yaml with intrinsic functions kept as opaque values."""
    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load((Path(__file__).parents[2] / "sam" / "template.yaml").read_text(), Loader=Loader)


def _index(table: str, name: str) -> dict:
    props = _template()["Resources"][table]["Properties"]
    return next(i for i in props["GlobalSecondaryIndexes"] if i["IndexName"] == name)


class TestIndexParity:
    """Principle #28: the names the code queries are the ones the template creates."""

    def test_attention_index(self):
        index = _index("PipelineTokensTable", att.INDEX)
        assert [k["AttributeName"] for k in index["KeySchema"]] == [att.KEY, att.DUE]
        assert index["Projection"]["NonKeyAttributes"] == list(att.PROJECTION)

    def test_asset_partition_index(self):
        index = _index("AssetEventsTable", PARTITION_INDEX)
        assert [k["AttributeName"] for k in index["KeySchema"]] == ["asset_name", "partition_date"]
        assert index["Projection"]["NonKeyAttributes"] == list(PARTITION_INDEX_PROJECTION)

    def test_index_items_carry_what_fact_from_item_reads(self):
        item = fact_item()
        projected = {k: item[k] for k in ("asset_name", "partition_date", *PARTITION_INDEX_PROJECTION)}
        assert fact_from_item(projected) == fact_from_item(item)


class TestAttention:
    def test_shard_is_the_first_hex_digit_of_sha256(self):
        name = "load-2026-11-12-run-2026-11-12-abc12345"
        assert att.shard(name) == hashlib.sha256(name.encode()).hexdigest()[0]
        assert att.attention(att.WAITING, name) == f"waiting#{att.shard(name)}"

    def test_sixteen_partitions_per_kind(self):
        assert att.partitions(att.RETRY) == tuple(f"retry#{h}" for h in "0123456789abcdef")

    @pytest.mark.parametrize("call", [lambda: att.attention("other", "x"), lambda: att.partitions("other")])
    def test_unknown_kinds_raise(self, call):
        with pytest.raises(ValueError):
            call()

    @pytest.mark.parametrize("attempts,minutes", [(0, 10), (1, 20), (2, 30), (9, 30)])
    def test_backoff(self, attempts, minutes):
        assert att.next_due(NOW, attempts) == NOW + timedelta(minutes=minutes)

    def test_negative_attempts_raise(self):
        with pytest.raises(ValueError):
            att.next_due(NOW, -1)


class TestWarningItem:
    def test_shape(self):
        item = warning_item(kind="zombie", execution_name="load-2026-11-12-x", task_name="load",
                            pipeline_name="p", pipeline_execution="p-run-2026-11-12-x",
                            task_date="2026-10-01", error="wrapper stopped", now=NOW)
        assert item["execution_name"] == "_notify_warn_zombie_load-2026-11-12-x"
        assert item["attention"] == att.attention(att.WARN, "load-2026-11-12-x")
        assert item["attention_at"] == item["finished_at"] == "2026-11-12T06:00:00.123Z"
        assert item["task_date"] == "2026-10-01"
        assert "date" not in item       # not listed again by the date-partitioned failure query
        assert item["ttl"] == int((NOW + timedelta(days=8)).timestamp())

    def test_unknown_context_is_left_out_because_empty_index_keys_are_rejected(self):
        item = warning_item(kind="coordination", execution_name="load-x", task_name="", pipeline_name="",
                            pipeline_execution="", task_date="", error="AccessDenied", now=NOW)
        assert not {"task_name", "pipeline_name", "pipeline_execution", "task_date"} & set(item)
        assert "" not in item.values()

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            warning_item(kind="wakeup", execution_name="x", task_name="t", pipeline_name="p",
                         pipeline_execution="e", task_date="d", error="e", now=NOW)


def fact_item(**overrides):
    args = dict(asset_name="ns/a", partition_key="2026-W46", granularity="weekly", cascade="auto",
                source_dag="producer", source_task="build", uri="s3://b/a", now=NOW, ttl_days=365)
    args.update(overrides)
    return asset_fact_item(**args)


class TestAssetFactItem:
    def test_shape(self):
        item = fact_item()
        assert item["partition_key"] == item["execution_date"] == "2026-W46"
        assert item["partition_date"] == "2026-11-09"     # Monday of ISO week 46
        assert item["fact_version"] == FACT_VERSION
        assert item["event_time"] == "2026-11-12T06:00:00.123Z"

    @pytest.mark.parametrize("overrides", [
        {"asset_name": " "}, {"granularity": "yearly"}, {"cascade": "some"}, {"ttl_days": 0},
        {"partition_key": "2026-13"},
    ])
    def test_invalid_values_raise(self, overrides):
        with pytest.raises(ValueError):
            fact_item(**overrides)


class TestFactFromItem:
    def test_new_rows_round_trip(self):
        fact = fact_from_item(fact_item())
        assert (fact.asset_name, fact.partition_date, fact.granularity) == ("ns/a", date(2026, 11, 9), "weekly")
        assert fact.event_time == NOW.replace(microsecond=123000)

    def test_index_items_need_no_execution_date(self):
        item = {k: v for k, v in fact_item().items() if k != "execution_date"}
        assert fact_from_item(item).partition_date == date(2026, 11, 9)

    def test_legacy_rows_use_execution_date_and_the_declared_granularity(self):
        legacy = {"asset_name": "ns/a", "event_time": "2026-11-12T06:00:00.123456+00:00", "execution_date": "2026-11-12"}
        fact = fact_from_item(legacy, declared_granularity="monthly")
        assert (fact.partition_date, fact.granularity) == (date(2026, 11, 12), "monthly")

    @pytest.mark.parametrize("key,granularity,partition", [
        ("2026-11-12", "daily", date(2026, 11, 12)),
        ("2026-11-12T06", "hourly", date(2026, 11, 12)),
        ("2026-W46", "weekly", date(2026, 11, 9)),
        ("2026-11", "monthly", date(2026, 11, 1)),
    ])
    def test_without_a_declared_granularity_the_key_format_decides(self, key, granularity, partition):
        legacy = {"asset_name": "ns/a", "event_time": "2026-11-12T06:00:00Z", "execution_date": key}
        fact = fact_from_item(legacy)
        assert (fact.granularity, fact.partition_date) == (granularity, partition)

    def test_the_stored_granularity_wins_over_the_declared_one(self):
        assert fact_from_item(fact_item(), declared_granularity="daily").granularity == "weekly"

    @pytest.mark.parametrize("stored", ["20261109", "2026-11-9", "2026-11-09T00"])
    def test_a_partition_date_outside_yyyy_mm_dd_raises(self, stored):
        """The index keys facts by YYYY-MM-DD: a fact stored otherwise is unreadable, never silently unreachable."""
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            fact_from_item({**fact_item(), "partition_date": stored})

    def test_an_unknown_granularity_raises(self):
        with pytest.raises(ValueError, match="yearly"):
            fact_from_item({**fact_item(), "granularity": "yearly"})


class TestPushRetryItem:
    def test_shape(self):
        item = push_retry_item(run_name="pushed-2026-11-12-and-0a1b2c3d", consumer="pushed",
                               sfn_arn="arn:aws:states:us-east-1:1:stateMachine:pushed", run_input='{"a": 1}',
                               error="ThrottlingException", now=NOW)
        assert item["execution_name"] == "_retry_push_pushed-2026-11-12-and-0a1b2c3d"
        assert item[att.KEY] == att.attention(att.RETRY, "pushed-2026-11-12-and-0a1b2c3d")
        assert item[att.DUE] == "2026-11-12T06:05:00.123Z"           # first retry 5 minutes later (ADR #130 §8)
        assert (item["attempts"], item["first_failed_at"]) == (0, "2026-11-12T06:00:00.123Z")
        assert item["ttl"] == int((NOW + timedelta(days=7)).timestamp())
        assert "date" not in item and "pipeline_execution" not in item

    @pytest.mark.parametrize("missing", ["run_name", "consumer", "sfn_arn"])
    def test_needs_what_a_restart_needs(self, missing):
        args = dict(run_name="r", consumer="c", sfn_arn="arn", run_input="{}", error="e", now=NOW)
        args[missing] = " "
        with pytest.raises(ValueError):
            push_retry_item(**args)
