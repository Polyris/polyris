"""run_task records asset facts as polyris.coordination.records shapes them (ADR #130 §1, §6; ADR #137 §1).

Two writers produce facts — run_task's success transaction, in JSONata, and the
console's write_fact — and one reader evaluates them. These tests evaluate
run_task's transaction and compare its facts with the Python the console and the
reader use.
"""

from datetime import date, timedelta

import pytest
from sfn_jsonata import NOW, evaluate, load_template, plain

from polyris.coordination.records import asset_fact_item

TTL_DAYS = 365
CASCADES = ("auto", "all", "none")


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


def _transaction(states, key="2026-11-12", *, outlets=None, cascade="auto"):
    run = {"execution_name": f"build-{key}-producer-run-x", "generation": 2, "task_name": "build",
           "pipeline_name": "producer", "date": key, "cascade": cascade,
           "outlets": outlets if outlets is not None else [{"name": "ns/orders", "uri": "s3://bucket/orders"}],
           "output_ttl_seconds": 31536000, "history_ttl_seconds": 39312000}
    arguments = states["Record_Success"]["Arguments"]
    return evaluate(arguments, input={"task_output": "{}", "_task_pushed": False},
                    context={"Execution": {"Input": run, "Id": "arn:run-task", "Name": "run-task"}})["TransactItems"]


def _record(states, key, *, granularity=None, cascade="auto"):
    outlet = {"name": "ns/orders", "uri": "s3://bucket/orders"}
    if granularity is not None:
        outlet["granularity"] = granularity
    _, _, fact = _transaction(states, key, outlets=[outlet], cascade=cascade)
    return plain(fact["Put"]["Item"])


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
    assert _record(states, key, granularity="")["granularity"] == granularity


def test_the_declared_granularity_wins(states):
    assert _record(states, "2026-11-10", granularity="weekly")["granularity"] == "weekly"


@pytest.mark.parametrize("cascade", CASCADES)
def test_the_facts_carry_the_cascade_of_their_run(states, cascade):
    """Start_Run keeps the run's cascade (records.cascade_of, as Mark Successful computes it, ADR #57) and the
    dispatcher passes it to run_task: one cascade per run, whoever records its facts."""
    facts = _transaction(states, outlets=[{"name": "ns/a"}, {"name": "ns/b"}], cascade=cascade)[2:]
    assert [plain(f["Put"]["Item"])["cascade"] for f in facts] == [cascade, cascade]


def test_a_suppressed_run_still_records_its_facts(states):
    """cascade='none' (ADR #57) suppresses push consumers only: pull consumers of the partition see the fact.
    run_task records it with cascade 'none'; AssetFanout starts nothing for it."""
    assert len(_transaction(states, cascade="none")) == 3


def test_run_task_starts_no_consumer_and_wakes_no_dependant_itself(states):
    """Push consumers start from the fact, in AssetFanout, and waiting tasks wake through the streams: none
    can fail the producer (B-F1, ADR #137 §7)."""
    calls = {s.get("Resource", "") for s in states.values()}
    assert not [r for r in calls if "startSyncExecution" in r or r.startswith("arn:aws:states:::states:")
                and not r.endswith(".sync:2")]
    lambdas = [s["Arguments"].get("FunctionName", "") for s in states.values()
               if s.get("Resource") == "arn:aws:states:::lambda:invoke"]
    assert not any("notify_asset_subscribers" in f or "query_subscriptions" in f for f in lambdas)


def test_a_success_never_lacks_its_facts(states):
    """The facts are in the success's own transaction (invariant 12): if it cannot be written, the row waits
    for a decision with the cause instead of succeeding without them."""
    record = states["Record_Success"]
    catches = {e: c["Next"] for c in record["Catch"] for e in c["ErrorEquals"]}
    assert catches["States.ALL"] == catches["States.DataLimitExceeded"] == "Success_Not_Recorded"
    assert "Emit_Asset_Events" not in states and "Warn_Facts_Not_Recorded" not in states


def test_a_fact_never_overwrites_another_of_the_same_millisecond(states):
    """The key is (asset_name, event_time): an overwrite would lose a fact and, being a MODIFY, never reach
    the INSERT-only stream mapping. A collision cancels the transaction, which runs again with a later $now()."""
    facts = _transaction(states, outlets=[{"name": "ns/a"}, {"name": "ns/b"}])[2:]
    assert {f["Put"]["ConditionExpression"] for f in facts} == {"attribute_not_exists(event_time)"}
    assert states["Wait_Before_Recording_Again"]["Next"] == "Record_Success"
    assert evaluate(states["Wait_Before_Recording_Again"]["Seconds"], variables={"record_tries": 1}) >= 1
