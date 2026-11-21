"""Fact reads, error classification and the clients of the coordination Lambdas."""

import json
from datetime import date

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError, ProxyConnectionError, ReadTimeoutError, ResponseStreamingError, SSLError
from coordination_testkit import NOW, TABLES, client_error, fact

import env
import failures
from stores import FactStore, Registry

@pytest.fixture
def facts(ddb):
    return FactStore(ddb, TABLES["ASSET_EVENTS_TABLE"][1], Registry(ddb, TABLES["PIPELINES_TABLE"][1]), NOW)


def test_facts_merge_the_index_and_recent_writes_newest_first(facts, asset_events):
    # event_time is the table's sort key, so every fact needs its own.
    asset_events.put_item(Item=fact("ns/a", "2026-11-10", event_time="2026-11-12T03:00:00.000Z"))
    asset_events.put_item(Item=fact("ns/a", "2026-11-12", event_time="2026-11-12T05:59:00.000Z"))   # in both reads
    asset_events.put_item(Item=fact("ns/a", "2026-11-13", event_time="2026-11-12T04:30:00.000Z"))   # after `last`
    found = list(facts.facts("ns/a", last=date(2026, 11, 12), first=date(2026, 11, 1)))
    assert [f.partition_date for f in found] == [date(2026, 11, 12), date(2026, 11, 10)]


def test_facts_without_a_lower_bound(facts, asset_events):
    asset_events.put_item(Item=fact("ns/a", "2025-01-01"))
    assert [f.partition_date for f in facts.facts("ns/a", last=date(2026, 11, 12), first=None)] == [date(2025, 1, 1)]


def test_fact_reads_follow_pagination(facts, mocker):
    item = fact("ns/a", "2026-11-12")
    pages = [{"Items": [item], "LastEvaluatedKey": {"k": 1}}, {"Items": []},      # recent (consistent) read
             {"Items": [item], "LastEvaluatedKey": {"k": 2}}, {"Items": []}]      # partition index
    query = mocker.patch.object(facts.table, "query", side_effect=pages)
    assert len(list(facts.facts("ns/a", last=date(2026, 11, 12), first=None))) == 1
    assert [c.kwargs.get("ExclusiveStartKey") for c in query.call_args_list] == [None, {"k": 1}, None, {"k": 2}]


# ── Registry ────────────────────────────────────────────────────────────


@pytest.fixture
def reg(ddb):
    return Registry(ddb, TABLES["PIPELINES_TABLE"][1])


@pytest.mark.parametrize("entry", [
    None,
    {"pipeline_name": "producer"},
    {"pipeline_name": "producer", "dag": json.dumps({"nodes": [{"outlets": ["ns/a"]}]})},
    {"pipeline_name": "producer", "dag": json.dumps({"nodes": [{"outlets": [{"name": "ns/b", "granularity": "weekly"}]}]})},
])
def test_no_declared_granularity(reg, registry, entry):
    if entry:
        registry.put_item(Item=entry)
    assert reg.declared_granularity("producer", "ns/a") is None


def test_a_declared_granularity_is_read_once_per_invocation(reg, registry, mocker):
    registry.put_item(Item={"pipeline_name": "producer",
                            "dag": json.dumps({"nodes": [{"outlets": [{"name": "ns/a", "granularity": "monthly"}]}]})})
    spy = mocker.spy(reg.table, "get_item")
    assert reg.declared_granularity("producer", "ns/a") == reg.declared_granularity("producer", "ns/a") == "monthly"
    assert spy.call_count == 1


def test_facts_without_a_producer_skip_the_registry(reg, mocker):
    spy = mocker.spy(reg.table, "get_item")
    assert reg.declared_granularity("", "ns/a") is None
    spy.assert_not_called()


def test_a_malformed_registry_entry_raises(reg, registry):
    registry.put_item(Item={"pipeline_name": "producer", "dag": "[]"})
    with pytest.raises(ValueError, match="expected an object"):
        reg.declared_granularity("producer", "ns/a")


# ── failures ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exc,transient", [
    (client_error("ThrottlingException"), True),
    (client_error("ServiceUnavailable"), True),
    (client_error("AccessDeniedException"), False),
    (client_error("ValidationException"), False),
    (ClientError({"Error": {"Code": "SomeNewServerError"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "Op"), True),
    (ClientError({"Error": {"Code": "AccessDeniedException"}, "ResponseMetadata": {"HTTPStatusCode": 400}}, "Op"), False),
    (EndpointConnectionError(endpoint_url="https://dynamodb"), True),
    (SSLError(endpoint_url="https://dynamodb", error="bad record mac"), True),       # botocore retries these
    (ProxyConnectionError(proxy_url="http://proxy:3128", error="refused"), True),
    (ResponseStreamingError(error="connection reset"), True),
    (ReadTimeoutError(endpoint_url="https://dynamodb"), True),
    (NoCredentialsError(), False),
    (ValueError("x"), False),
])
def test_transient_classification(exc, transient):
    assert failures.is_transient(exc) is transient


def test_the_transient_error_keeps_the_name_the_state_machines_retry_on():
    from polyris.coordination import wake

    assert failures.CoordinationTransient.__name__ == wake.TRANSIENT_ERROR


def test_non_aws_errors_have_no_code_and_are_not_re_raised():
    assert failures.error_code(ValueError("x")) == ""
    failures.reraise_transient(ValueError("x"))     # no exception


# ── env ─────────────────────────────────────────────────────────────────


def test_clients_are_created_once(ddb, monkeypatch):
    monkeypatch.setattr(env, "_dynamodb", None)
    monkeypatch.setattr(env, "_stepfunctions", None)
    assert env.dynamodb() is env.dynamodb()
    assert env.stepfunctions() is env.stepfunctions()


def test_every_client_fails_fast_inside_the_lambda_timeout(ddb, monkeypatch):
    for name in ("_dynamodb", "_stepfunctions", "_lambda"):
        monkeypatch.setattr(env, name, None)
    for client in (env.dynamodb().meta.client, env.stepfunctions(), env.lambda_()):
        config = client.meta.config
        assert config.retries == {"mode": "standard", "total_max_attempts": 3}
        assert (config.connect_timeout, config.read_timeout) == (2, 5)
