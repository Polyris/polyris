"""DynamoDB stores, signals and error classification of the coordination Lambdas."""

import json
from datetime import date, timedelta

import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError
from coordination_testkit import NOW, STARTED_AT, TABLES, FakeSfn, client_error, fact, task_row

import env
import failures
from coordination_core import attention as att
from failures import CoordinationTransient
from signals import ACCEPTED, DEAD, Signals
from stores import NOT_FOUND, FactStore, Registry, TaskStore

TOKENS = TABLES["TOKENS_TABLE"][1]


@pytest.fixture
def store(ddb):
    return TaskStore(ddb, TOKENS)


# ── TaskStore ───────────────────────────────────────────────────────────


def test_dependency_facts_read_every_key_in_chunks(store, tokens):
    names = [f"task{i}" for i in range(150)]
    tokens.put_item(Item={"execution_name": "task7", "status": "success"})
    tokens.put_item(Item={"execution_name": "task120", "status": "skipped", "skip_origin": "rule",
                          "finished_at": "2026-11-12T05:00:00.000Z"})
    facts = store.dependency_facts(names)
    assert len(facts) == 150
    assert facts["task7"] == ("success", "", "")
    assert facts["task120"] == ("skipped", "rule", "2026-11-12T05:00:00.000Z")
    assert facts["task0"] == (NOT_FOUND, "", "")


def test_unprocessed_keys_are_retried(store, ddb, tokens, mocker):
    tokens.put_item(Item={"execution_name": "extract", "status": "success"})
    real = ddb.batch_get_item
    pending = {TOKENS: {"Keys": [{"execution_name": "extract"}]}}
    calls = iter([{"Responses": {TOKENS: []}, "UnprocessedKeys": pending}])
    mocker.patch.object(ddb, "batch_get_item", side_effect=lambda **kw: next(calls, None) or real(**kw))
    sleep = mocker.patch("stores.time.sleep")
    assert store.dependency_facts(["extract"])["extract"] == ("success", "", "")
    sleep.assert_called_once()


def test_keys_that_stay_unprocessed_fail_closed(store, ddb, mocker):
    pending = {TOKENS: {"Keys": [{"execution_name": "extract"}]}}
    mocker.patch.object(ddb, "batch_get_item", return_value={"Responses": {}, "UnprocessedKeys": pending})
    mocker.patch("stores.time.sleep")
    with pytest.raises(CoordinationTransient):
        store.dependency_facts(["extract"])


def test_clear_attention_on_a_row_without_started_at(store, tokens):
    tokens.put_item(Item={"execution_name": "x", "status": "success", att.KEY: "waiting#1", att.DUE: "t"})
    assert store.clear_attention("x", "success", None) is True
    assert att.KEY not in tokens.get_item(Key={"execution_name": "x"})["Item"]


@pytest.mark.parametrize("status,started_at", [("running", STARTED_AT), ("success", "2026-11-12T07:00:00.000Z")])
def test_clear_attention_never_touches_a_row_that_moved_on(store, tokens, status, started_at):
    tokens.put_item(Item=task_row("load", status="success", **{att.KEY: "waiting#1", att.DUE: "t"}))
    name = task_row("load")["execution_name"]
    assert store.clear_attention(name, status, started_at) is False
    assert store.clear_attention("missing-row", "success", STARTED_AT) is False   # never creates a phantom row
    assert tokens.get_item(Key={"execution_name": "missing-row"}).get("Item") is None


def test_mark_due_records_attempts_and_needs_an_attention_key(store, tokens):
    tokens.put_item(Item=task_row("load", **{att.KEY: "waiting#1", att.DUE: "t"}))
    tokens.put_item(Item=task_row("report"))
    load, report = task_row("load")["execution_name"], task_row("report")["execution_name"]
    assert store.mark_due(load, STARTED_AT, NOW + timedelta(minutes=20), attempts=2) is True
    item = tokens.get_item(Key={"execution_name": load})["Item"]
    assert (item[att.DUE], item["attempts"]) == ("2026-11-12T06:20:00.000Z", 2)
    assert store.mark_due(report, STARTED_AT, NOW) is False


def test_park_is_pinned_to_token_and_registration(store, tokens):
    tokens.put_item(Item=task_row("load"))
    name = task_row("load")["execution_name"]
    assert store.park(name, "token-other", STARTED_AT) is False
    assert store.park(name, "token-load", "2026-11-12T00:00:00.000Z") is False
    assert store.park(name, "token-load", STARTED_AT) is True
    assert store.park(name, "token-load", STARTED_AT) is False      # already parked


def test_run_rows_follow_pagination(store, mocker):
    page1 = {"Items": [task_row("load")], "LastEvaluatedKey": {"execution_name": "load"}}
    page2 = {"Items": [task_row("extract", status="running"), task_row("report", status="waiting_paused")]}
    query = mocker.patch.object(store.table, "query", side_effect=[page1, page2])
    assert [r["task_name"] for r in store.run_rows("run")] == ["load", "report"]
    assert query.call_args_list[1].kwargs["ExclusiveStartKey"] == {"execution_name": "load"}


# ── FactStore ───────────────────────────────────────────────────────────


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


# ── Signals ─────────────────────────────────────────────────────────────


def test_signals_classify_dead_tokens():
    sfn = FakeSfn()
    sfn.dead_tokens.add("t")
    assert Signals(sfn).succeed("t", {}) == DEAD and Signals(sfn).fail("t", "x") == DEAD
    assert Signals(sfn).succeed("u", {"signal": "deps_ready"}) == ACCEPTED


def test_a_long_cause_is_cut_to_the_service_limit():
    sfn = FakeSfn()
    Signals(sfn).fail("t", "x" * 5000)
    assert len(sfn.failed[0]["cause"]) == 2000


@pytest.mark.parametrize("code,raised", [("ThrottlingException", CoordinationTransient), ("AccessDeniedException", Exception)])
def test_describe_execution_errors(code, raised):
    sfn = FakeSfn()
    sfn.errors["describe_execution"] = client_error(code)
    with pytest.raises(raised):
        Signals(sfn).execution_status("arn")


def test_a_missing_execution_has_no_status():
    assert Signals(FakeSfn()).execution_status("arn:missing") is None


# ── failures ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exc,transient", [
    (client_error("ThrottlingException"), True),
    (client_error("ServiceUnavailable"), True),
    (client_error("AccessDeniedException"), False),
    (client_error("ValidationException"), False),
    (EndpointConnectionError(endpoint_url="https://dynamodb"), True),
    (NoCredentialsError(), False),
    (ValueError("x"), False),
])
def test_transient_classification(exc, transient):
    assert failures.is_transient(exc) is transient


def test_the_transient_error_keeps_the_name_the_state_machines_retry_on():
    from coordination_core import wake

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
