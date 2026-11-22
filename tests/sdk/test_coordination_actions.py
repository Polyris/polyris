"""An operator's action on a task, built from the console's reads (ADR #137 §3; coordination.actions)."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from polyris.constants import TaskStatus
from polyris.coordination import actions, attention, records, retention, run_name, run_row, task_row, task_spec
from polyris.coordination.times import format_utc

S = TaskStatus
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)
TOKENS, FACTS = "tokens", "asset-events"
RUN = "orders-run-2026-11-21-abcd1234"
KEY = "load-2026-11-21--2026-11-21-abcd1234"
START = "2026-11-21T06:00:00.123Z"
OUTPUT = "output#orders#load#2026-11-21"
_ser, _de = TypeSerializer(), TypeDeserializer()


@pytest.fixture
def ddb():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName=TOKENS, KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        client.create_table(TableName=FACTS, KeySchema=[{"AttributeName": "asset_name", "KeyType": "HASH"},
                                                        {"AttributeName": "event_time", "KeyType": "RANGE"}],
                            AttributeDefinitions=[{"AttributeName": "asset_name", "AttributeType": "S"},
                                                  {"AttributeName": "event_time", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        yield client


def task(status: str, *, generation: int = 2, error: Optional[str] = None) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        task_row.TABLE_KEY: KEY, task_row.TASK_NAME: "load", task_row.STATUS: status,
        task_row.GENERATION: generation, "pipeline_name": "orders", task_row.BACKFILL_ID: "",
        task_row.READINESS: task_spec.canonical_json({"trigger_rule": "all_success", "dependencies": ["extract"]})}
    if error:
        item[task_row.ERROR] = error
    return item


def run(*, version: int = 5, due: Optional[str] = None) -> Dict[str, Any]:
    item = {run_row.TABLE_KEY: run_row.key(RUN), run_row.EXECUTION: RUN, run_row.STATUS: run_row.ACTIVE,
            run_row.RUN_VERSION: version, run_row.RUN_START: START, run_row.RUN_DATE: "2026-11-21",
            run_row.PIPELINE: "orders"}
    if due:
        item[attention.KEY], item[attention.DUE] = attention.attention(attention.WAITING, run_row.key(RUN)), due
    return item


def put(ddb, item: Dict[str, Any]) -> None:
    ddb.put_item(TableName=TOKENS, Item={k: _ser.serialize(v) for k, v in item.items()})


def get(ddb, key: str, table: str = TOKENS) -> Dict[str, Any]:
    item = ddb.get_item(TableName=table, Key={"execution_name": {"S": key}}, ConsistentRead=True).get("Item")
    return {k: _de.deserialize(v) for k, v in (item or {}).items()}


def build(action: str, row: Dict[str, Any], *, output: Optional[Dict[str, Any]] = None, outlets=(), reason="why",
          run_item: Optional[Dict[str, Any]] = None) -> actions.ActionWrite:
    return actions.task_action(action=action, task=row, run=run_item or run(), output=output, outlets=outlets,
                               cascade="auto", operator="ops@example.com", reason=reason, now=NOW,
                               tokens_table=TOKENS, asset_events_table=FACTS, fact_ttl_days=365)


def send(ddb, write: actions.ActionWrite) -> bool:
    try:
        ddb.transact_write_items(TransactItems=write.items)
        return True
    except ddb.exceptions.TransactionCanceledException:
        return False


def seed(ddb, row: Dict[str, Any], output: Optional[Dict[str, Any]] = None, run_item=None) -> None:
    put(ddb, row)
    put(ddb, run_item or run())
    if output is not None:
        put(ddb, output)


@pytest.mark.parametrize("action, status, skip_origin", [("skip", S.SKIPPED.value, "manual"),
                                                         ("mark_success", S.SUCCESS.value, None),
                                                         ("fail", S.FAILED.value, None),
                                                         ("stop", S.STOPPED.value, None)])
def test_an_action_writes_its_row_its_run_row_and_its_marker_in_one_transaction(ddb, action, status, skip_origin):
    row = task(S.WAITING_DECISION.value, error="the work failed")
    seed(ddb, row)
    assert send(ddb, build(action, row))
    written = get(ddb, KEY)
    assert (written["status"], written["generation"], written.get("skip_origin")) == (status, 3, skip_origin)
    assert written["finished_at"] == format_utc(NOW)
    assert ("attention" in written) is (status == S.FAILED.value)                  # the bell shows a failure
    assert written.get("error") == ("why" if action == "fail" else "the work failed")
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.RUN_VERSION] == 6 and the_run[attention.DUE] == format_utc(NOW)   # due at once
    marker = json.loads(get(ddb, OUTPUT)["result"])
    assert marker == {"_manually_resolved": True, "_resolution": action, "_reason": "why",
                      "_operator": "ops@example.com", "_pipeline_execution": RUN}
    assert int(get(ddb, OUTPUT)["ttl"]) == retention.ttl(NOW, retention.OUTPUT)


def test_a_run_rows_due_time_and_a_raised_run_version_are_kept(ddb):
    """An action makes the run row due only when it has none, and raises run_version whatever it is (§3)."""
    row = task(S.FAILED.value)
    seed(ddb, row, run_item=run(version=9, due="2026-11-22T13:00:00.000Z"))
    assert send(ddb, build("skip", row, run_item=run(version=4)))        # read an older version: no matter
    the_run = get(ddb, run_row.key(RUN))
    assert the_run[run_row.RUN_VERSION] == 10 and the_run[attention.DUE] == "2026-11-22T13:00:00.000Z"


def test_an_action_on_a_row_that_moved_since_its_read_is_refused(ddb):
    """The row's write is conditional on the status and the generation read (invariant 5)."""
    seed(ddb, task(S.RUNNING.value, generation=3))
    assert not send(ddb, build("stop", task(S.RUNNING.value, generation=2)))
    assert get(ddb, KEY)["status"] == S.RUNNING.value and get(ddb, run_row.key(RUN))[run_row.RUN_VERSION] == 5


def test_real_output_keeps_the_marker_out_and_says_so(ddb):
    """Principle #31: an earlier run's output of the date stays; the console warns instead of writing over it."""
    row, output = task(S.FAILED.value), {"execution_name": OUTPUT, "result": json.dumps({"rows": 3})}
    seed(ddb, row, output)
    write = build("mark_success", row, output=output)
    assert write.marker_blocked and send(ddb, write)
    assert json.loads(get(ddb, OUTPUT)["result"]) == {"rows": 3}


@pytest.mark.parametrize("result", ["ok", 7])
def test_a_1_2_string_result_or_any_other_shape_is_real_output(result):
    assert build("skip", task(S.FAILED.value), output={"execution_name": OUTPUT, "result": result}).marker_blocked


def test_an_earlier_marker_is_replaced_and_a_push_since_the_read_cancels_the_action(ddb):
    row = task(S.FAILED.value)
    earlier = {"execution_name": OUTPUT, "result": json.dumps({"_manually_resolved": True, "_resolution": "fail"})}
    seed(ddb, row, earlier)
    put(ddb, {"execution_name": OUTPUT, "result": json.dumps({"pushed": 1})})        # xcom.push after the read
    assert not send(ddb, build("skip", row, output=earlier))
    assert get(ddb, KEY)["status"] == S.FAILED.value
    put(ddb, earlier)
    assert send(ddb, build("skip", row, output=earlier))
    assert json.loads(get(ddb, OUTPUT)["result"])["_resolution"] == "skip"


def test_mark_successful_records_a_fact_per_outlet_in_its_transaction(ddb):
    row = task(S.FAILED.value)
    seed(ddb, row)
    outlets = [{"name": "ns/orders", "uri": "s3://lake/orders/"}, {"name": "ns/weekly", "granularity": "weekly"}]
    assert send(ddb, build("mark_success", row, outlets=outlets))
    facts = {i["asset_name"]["S"]: {k: _de.deserialize(v) for k, v in i.items()}
             for i in ddb.scan(TableName=FACTS)["Items"]}
    expected = records.asset_fact_item(asset_name="ns/orders", partition_key="2026-11-21", granularity="daily",
                                       cascade="auto", source_dag="orders", source_task="load",
                                       uri="s3://lake/orders/", now=NOW, ttl_days=365)
    assert facts["ns/orders"] == expected and facts["ns/weekly"]["granularity"] == "weekly"
    assert not build("skip", task(S.FAILED.value), outlets=outlets).items[3:]          # only Mark Successful
    assert len(build("mark_success", task(S.FAILED.value), outlets=[{"uri": "s3://x/"}]).items) == 3   # no name


def test_stop_names_the_run_task_of_the_generation_it_read_and_the_events_follow(ddb):
    write = build("stop", task(S.RUNNING.value, generation=2, error="earlier"))
    assert write.run_task_to_stop == run_name.run_task_name(KEY, START, 2)
    assert build("stop", task(S.WAITING.value)).run_task_to_stop is None             # nothing runs yet
    finished, decision = write.events
    assert (finished["event_type"], finished["status"]) == ("TASK_FINISHED", "failed")
    assert (decision["event_type"], decision["decision"], decision["operator"]) == ("MANUAL_DECISION", "stop",
                                                                                     "ops@example.com")
    assert finished["task_run_id"] == decision["task_run_id"] == run_name.run_task_name(KEY, START, 2)
    assert [e["event_type"] for e in build("fail", task(S.RUNNING.value, error="x")).events] == ["MANUAL_DECISION"]


@pytest.mark.parametrize("action, status", [("skip", S.SUCCESS.value), ("mark_success", S.SKIPPED.value),
                                            ("fail", S.FAILED.value), ("stop", S.STOPPED.value)])
def test_an_action_the_table_does_not_allow_from_the_status_is_refused(action, status):
    with pytest.raises(actions.ActionRefused, match=f"takes no {action}"):
        build(action, task(status))


def test_a_decision_without_a_reason_says_how_it_was_taken():
    [decision] = build("skip", task(S.FAILED.value), reason="").events
    assert decision["reason"] == "Manual skip via UI"


def test_only_the_light_actions_are_built_here():
    with pytest.raises(ValueError, match="no such action"):
        build("restart", task(S.FAILED.value))


def test_a_task_of_a_run_from_before_1_3_takes_no_action():
    legacy = {k: v for k, v in task(S.FAILED.value).items() if k != task_row.READINESS}
    with pytest.raises(actions.ActionRefused, match="before 1.3"):
        build("skip", legacy)


def test_the_marker_fields_are_those_xcom_reads():
    """Principle #28: xcom.py, copied alone into a Lambda's package, spells the field out."""
    from polyris import xcom
    assert actions.MARKER_RESOLVED == xcom._MANUAL_RESOLVED_FIELD


def test_a_mark_successful_transaction_fits_dynamodb_with_the_most_outlets():
    """The row, the run row, the marker and a fact per outlet: at most 100 items (§8)."""
    outlets = [{"name": f"ns/a{i}"} for i in range(task_spec.MAX_OUTLETS)]
    assert len(build("mark_success", task(S.FAILED.value), outlets=outlets).items) <= 100
