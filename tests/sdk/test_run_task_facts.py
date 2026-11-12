"""run_task records asset facts as polyris.coordination.records shapes them (ADR #130 §1, §6).

Two writers produce facts — run_task's JSONata and the console's
write_fact — and one reader evaluates them. These tests evaluate run_task's
expressions and compare them with the Python the console and the reader use.
"""

from datetime import date, timedelta

import pytest
from sfn_jsonata import NOW, evaluate, find_state, load_template, plain

from polyris.coordination.records import asset_fact_item, warning_item

TTL_DAYS = 365


@pytest.fixture(scope="module")
def states():
    return load_template("helpers/run_task/sfn.tpl.json",
                         {"asset_event_ttl_days": TTL_DAYS, "asset_events_table": "asset-events",
                          "tokens_table": "pipeline-tokens"})["States"]


def _partition_keys():
    keys = ["2026-11-12", "2026-11-12T06", "2026-11-12T23", "2026-11", "2026-01", "2026-12"]
    for year in range(2020, 2029):
        last = date(year, 12, 28).isocalendar()[1]          # 52 or 53
        keys += [f"{year}-W{week:02d}" for week in range(1, last + 1)]
        keys += [f"{year}-{month:02d}" for month in range(1, 13)]
    keys += [(date(2026, 1, 1) + timedelta(days=d)).isoformat() for d in range(0, 366, 7)]
    return keys


def _record(states, key, *, granularity="", cascade="auto"):
    record = find_state(states, "Record_Asset_Event")["Arguments"]["Item"]
    run = {"date": key, "task_name": "build", "pipeline_name": "producer"}
    item = {"name": "ns/orders", "uri": "s3://bucket/orders", "granularity": granularity, "cascade": cascade}
    return plain(evaluate(record, input=item, context={"Execution": {"Input": run}}))


@pytest.mark.parametrize("key", _partition_keys())
def test_a_fact_has_the_shape_of_asset_fact_item(states, key):
    written = _record(states, key)
    expected = asset_fact_item(asset_name="ns/orders", partition_key=key,
                               granularity=written["granularity"], cascade="auto", source_dag="producer",
                               source_task="build", uri="s3://bucket/orders", now=NOW, ttl_days=TTL_DAYS)
    assert written == expected


@pytest.mark.parametrize("key,granularity", [
    ("2026-11-12", "daily"), ("2026-11-12T06", "hourly"), ("2026-W46", "weekly"), ("2026-11", "monthly"),
])
def test_without_a_declared_granularity_the_key_format_decides(states, key, granularity):
    assert _record(states, key)["granularity"] == granularity


def test_the_declared_granularity_wins(states):
    assert _record(states, "2026-11-10", granularity="weekly")["granularity"] == "weekly"


@pytest.mark.parametrize("flags,cascade", [
    ({}, "auto"), ({"cascade_all": True}, "all"), ({"_suppress_asset_event": True}, "none"),
    ({"_suppress_asset_event": True, "cascade_all": True}, "none"),
])
def test_cascade_follows_the_run(states, flags, cascade):
    emit = find_state(states, "Emit_Asset_Events")
    outlets = [{"name": "ns/a", "uri": "s3://a", "granularity": "weekly"}, {"name": "ns/b"}]
    items = evaluate(emit["Items"], input={"outlets": outlets, **flags})
    assert items == [{"name": "ns/a", "uri": "s3://a", "granularity": "weekly", "cascade": cascade},
                     {"name": "ns/b", "uri": "", "granularity": "", "cascade": cascade}]


def test_a_suppressed_run_still_records_its_facts(states):
    """cascade='none' (ADR #57) suppresses push consumers only: pull consumers of the partition see the fact.
    run_task records it with cascade 'none'; AssetFanout starts nothing for it."""
    [rule] = states["Check_Has_Outlets"]["Choices"]
    outlets = [{"name": "ns/a"}]
    assert evaluate(rule["Condition"], input={"outlets": outlets, "_suppress_asset_event": True}) is True
    assert rule["Next"] == "Emit_Asset_Events"


def test_run_task_starts_no_consumer_itself(states):
    """Push consumers start from the fact, in AssetFanout: none can fail the producer (B-F1)."""
    processor = states["Emit_Asset_Events"]["ItemProcessor"]["States"]
    assert set(processor) == {"Record_Asset_Event"}
    assert processor["Record_Asset_Event"].get("End") is True


def test_waiting_tasks_are_woken_by_the_stream_not_by_run_task(states):
    lambdas = [s["Arguments"].get("FunctionName", "") for s in states.values()
               if s.get("Resource") == "arn:aws:states:::lambda:invoke"]
    assert not any("notify_asset_subscribers" in f for f in lambdas)
    assert states["Emit_Asset_Events"]["Next"] == "Check_Orchestration_Token_Success"


def test_a_failed_notify_dependents_never_fails_the_producer(states):
    """ADR #130 §6.2 (B-F1): the warning reaches the bell, the producer finishes."""
    [catch] = states["Notify_Dependents"]["Catch"]
    assert catch["Next"] == "Warn_Notify_Dependents_Failed"
    warn = states["Warn_Notify_Dependents_Failed"]
    assert warn["Next"] == "Check_Has_Outlets"
    assert all(c["Next"] == "Check_Has_Outlets" for c in warn["Catch"])


def test_the_notify_warning_has_the_shape_of_warning_item(states):
    row = {"execution_name": "build-2026-11-12--2026-11-12-abcdef12", "task_name": "build",
           "pipeline_name": "producer", "pipeline_execution": "producer-run-2026-11-12-abcdef12",
           "date": "2026-11-12"}
    error = {"Error": "StepFunctions.ExecutionLimitExceeded", "Cause": "too many"}
    written = plain(evaluate(states["Warn_Notify_Dependents_Failed"]["Arguments"]["Item"],
                             input=row, variables={"notify_error": error}))
    expected = warning_item(kind="notify", execution_name=row["execution_name"], task_name="build",
                            pipeline_name="producer", pipeline_execution=row["pipeline_execution"],
                            task_date="2026-11-12", error=written["error"], now=NOW)
    assert written == expected
    assert "StepFunctions.ExecutionLimitExceeded" in written["error"]


def test_a_fact_never_overwrites_another_of_the_same_millisecond(states):
    """The key is (asset_name, event_time): an overwrite would lose a fact and, being a
    MODIFY, never reach the INSERT-only stream mapping. A collision retries with a later $now()."""
    record = find_state(states, "Record_Asset_Event")
    assert record["Arguments"]["ConditionExpression"] == "attribute_not_exists(event_time)"
    first = record["Retry"][0]
    assert first["ErrorEquals"] == ["DynamoDB.ConditionalCheckFailedException"]
    assert first["IntervalSeconds"] >= 1          # a later attempt evaluates $now() again
