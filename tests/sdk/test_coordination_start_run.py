"""What Start_Run writes, and the conditions of its writes (ADR #137 §1, §3)."""

import json
from typing import Any, Dict, Set
from datetime import datetime, timedelta, timezone

import pytest

from polyris.coordination import attention, deadlines, records, retention, run_input, run_row, start_run, task_row, task_spec

NOW = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)
EXECUTION = "orders-run-2026-11-19-abcd1234"
START = "2026-11-19T06:00:00.123Z"


def spec(**fields):
    base = {"task_id": "t", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"}
    text = task_spec.canonical_json({**base, **fields})
    return task_spec.parse(text, expected_hash=task_spec.text_hash(text))


SPECS = {
    "extract": spec(task_id="extract"),
    "watch": spec(task_id="watch", wait_for=[{"asset_name": "s3/orders"}]),
    "load": spec(task_id="load", dependencies=["extract"], orchestration_timeout=3600),
    "audit": spec(task_id="audit", dependencies=["extract"]),
}


def call(**changes):
    payload = {"format_version": task_spec.FORMAT_VERSION, "spec_id": "orders#" + "f" * 64, "task_count": 4,
               "pipeline_name": "orders", "execution_name": EXECUTION, "execution_arn": f"arn:aws:states:us-east-1:123456789012:execution:orders:{EXECUTION}", "execution_start": START,
               "token": "token-1", "retry_count": 0, "redrive_count": 0, "date": "2026-11-18",
               "input": {"skip_tasks": ["audit"], "triggered_by": "schedule"}}
    payload.update(changes)
    return start_run.call_from(payload)


# ── the call ─────────────────────────────────────────────────────────

def test_a_format_this_platform_does_not_run_is_refused_first_with_the_fix():
    with pytest.raises(start_run.FormatError, match="redeploy it with polyris-deploy"):
        call(format_version=0, spec_id=None)


@pytest.mark.parametrize("change,problem", [({"spec_id": None, "token": None}, "malformed"),
                                            ({"retry_count": "x"}, "malformed"), ({"task_count": 0}, "no task"),
                                            ({"date": None}, "malformed"), ({"date": ""}, "no date")])
def test_a_malformed_call_is_refused(change, problem):
    payload = {"format_version": task_spec.FORMAT_VERSION, "spec_id": "s", "task_count": 1, "pipeline_name": "p",
               "execution_name": "e", "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:orders:e", "execution_start": START, "token": "t", "retry_count": 0,
               "date": "2026-11-19", **change}
    for key in [k for k, v in change.items() if v is None]:
        payload.pop(key)
    with pytest.raises(ValueError, match=problem):
        start_run.call_from(payload)


def test_the_cascade_flags_of_the_payload_are_those_records_reads():
    assert records.cascade_of({start_run.RunArg.CASCADE_ALL: True}) == "all"
    assert records.cascade_of({start_run.RunArg.SUPPRESS_ASSET_EVENT: True, start_run.RunArg.CASCADE_ALL: True}) == "none"


def test_no_retry_of_start_run_matches_the_answer_of_a_failed_run():
    """TR-0 B4: a Retry that matched it would start the run again after its end."""
    from sfn_jsonata import LAMBDA_TRANSIENT

    assert set(start_run.RETRY_ERRORS) == LAMBDA_TRANSIENT            # as the platform's own templates retry
    assert run_row.FAILED_RUN_ERROR not in start_run.RETRY_ERRORS
    assert not {"States.ALL", "States.TaskFailed"} & set(start_run.RETRY_ERRORS)


def test_the_retry_window_holds_every_attempt_and_every_backoff_at_its_longest():
    assert start_run.TIMEOUT == timedelta(seconds=300)
    # 7 attempts of 300 s, and backoffs of 2, 4, 8, 16, 32 and 60 s (the last one capped).
    assert start_run.retry_window() == timedelta(seconds=7 * 300 + 2 + 4 + 8 + 16 + 32 + 60)


# ── the rows Start_Run writes ────────────────────────────────────────

def items_by_task():
    return {item["task_name"]: item for item in start_run.task_items(call(), SPECS, now=NOW)}


def test_each_task_gets_a_row_of_its_run_with_its_readiness_fields_and_spec():
    rows = items_by_task()
    load = rows["load"]
    assert load[task_row.TABLE_KEY] == task_row.key("load", "2026-11-18", EXECUTION)
    assert (load["status"], load["generation"], load["run_start"]) == ("waiting", 1, START)
    assert json.loads(load[task_row.READINESS]) == SPECS["load"].readiness_fields()
    assert load["spec_hash"] == SPECS["load"].text_hash and load["task_date"] == "2026-11-18"


def test_each_row_is_stamped_when_it_was_written_as_the_console_orders_by_it():
    """The date indexes project started_at; the pipeline page picks a date's latest run and the Tasks tab pages
    by it, as they did with 1.2's rows (the review of TR-5, "started_at without a writer")."""
    assert {item["started_at"] for item in items_by_task().values()} == {"2026-11-19T06:00:00.000Z"}


def test_a_task_the_run_skips_is_skipped_with_the_run_as_its_origin():
    audit = items_by_task()["audit"]
    assert (audit["status"], audit["skip_origin"]) == ("skipped", "run")
    assert attention.KEY not in audit


def test_a_skip_list_naming_an_unknown_task_is_refused():
    with pytest.raises(ValueError, match=r"does not have: \['ghost'\]"):
        start_run.task_items(call(input={"skip_tasks": ["ghost"]}), SPECS, now=NOW)


def test_only_a_row_with_a_time_of_its_own_is_due():
    rows = items_by_task()
    assert rows["watch"][attention.DUE] == "2026-11-26T06:00:00.000Z"   # a root task's wait_for: 7 days
    assert rows["load"][attention.DUE] == "2026-11-19T07:00:00.000Z"    # its orchestration_timeout
    assert attention.KEY not in rows["extract"]                          # it waits for nothing with a limit
    times = task_row.row_times(rows["watch"])
    assert deadlines.due_time(times, ()) == NOW + deadlines.WAIT_FOR_LIMIT


def test_a_waiting_task_with_wait_for_gets_its_asset_pointers_with_the_history_ttl():
    """The pointers are part of the run's history and live as long (ADR #137 §5, invariant 11)."""
    assert start_run.pointer_items(call(), SPECS, now=NOW) == [
        {"dependency_key": "asset:s3/orders", "subscriber": task_row.key("watch", "2026-11-18", EXECUTION),
         "run": run_row.key(EXECUTION), "ttl": retention.ttl(NOW, retention.HISTORY)}]


def test_the_runs_input_item_names_its_run_as_the_run_row_does():
    item = start_run.input_item(call(input={"variables": {"limit": 1.0}, "cascade_all": True}), now=NOW)
    assert run_input.from_item(item) == run_input.RunInput({"limit": 1.0}, (), "all", "2026-11-18")
    assert start_run.is_this_run(item, call()) and task_row.PIPELINE_EXECUTION not in item


def test_the_runs_input_keeps_the_partition_arg_its_tasks_get():
    """1.2 gave each task PARTITION_ARG: the run's own, or its date (generators.JSONATA_PARTITION)."""
    given = start_run.input_item(call(input={start_run.RunArg.PARTITION_ARG: "2026-W47"}), now=NOW)
    assert run_input.from_item(given).partition_arg == "2026-W47"
    assert run_input.from_item(start_run.input_item(call(input={}), now=NOW)).partition_arg == "2026-11-18"


def test_a_run_date_that_is_no_partition_key_is_refused_before_any_write():
    """A pass could read no fact for such a date (the review of TR-4)."""
    with pytest.raises(ValueError, match="no partition key"):
        call(date="2026/11/18")


def _index_keys() -> Dict[str, Set[str]]:
    """The key attributes of each index of the tasks table (sam/template.yaml): an item enters an index only
    when it carries every one of them."""
    from pathlib import Path

    from cfnlint.decode import decode

    template, matches = decode(str(Path(__file__).resolve().parents[2] / "sam" / "template.yaml"))
    assert template is not None, matches
    [table] = [res["Properties"] for res in template["Resources"].values()
               if res.get("Type") == "AWS::DynamoDB::Table" and "pipeline-tokens" in str(res["Properties"].get("TableName"))]
    return {index["IndexName"]: {k["AttributeName"] for k in index["KeySchema"]}
            for index in table["GlobalSecondaryIndexes"]}


def _set_names(update: Dict[str, Any]) -> Set[str]:
    """The attributes an UpdateExpression's SET clause writes."""
    import re

    names = update.get("ExpressionAttributeNames", {})
    clause = update["UpdateExpression"].split(" REMOVE ")[0].removeprefix("SET ")
    return {names.get(left.strip(), left.strip()) for left in re.findall(r"(#?\w+)\s*=", clause)}


def test_each_item_start_run_writes_enters_only_the_indexes_meant_for_it():
    """§3: the run's input enters no index; the run row only attention-index and run-start-index; a task row the
    indexes of a run's rows and of a date (the review of TR-4)."""
    indexes = _index_keys()

    def entered(attributes: Set[str]) -> Set[str]:
        return {name for name, keys in indexes.items() if keys <= attributes}

    the_call = call()
    assert entered(set(start_run.input_item(the_call, now=NOW))) == set()
    run_indexes = entered(_set_names(start_run.run_row_write(the_call, now=NOW)))
    assert run_indexes == {"attention-index", "run-start-index"}
    for item in start_run.task_items(the_call, SPECS, now=NOW):
        assert {"pipeline-execution-index", "date-pipeline-index", "pipeline-date-index"} <= entered(set(item))
        assert entered(set(item)) - {"pipeline-execution-index", "date-pipeline-index", "pipeline-date-index",
                                     "attention-index"} == set()


# ── the conditions, as DynamoDB evaluates them (moto) ────────────────

@pytest.fixture
def table():
    import boto3
    from moto import mock_aws

    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")


def write_run_row(table, the_call):
    from botocore.exceptions import ClientError

    try:
        table.update_item(**start_run.run_row_write(the_call, now=NOW))
        return True
    except ClientError as exc:
        assert exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
        return False


def run_item(table):
    return table.get_item(Key={"execution_name": run_row.key(EXECUTION)})["Item"]


def test_the_run_row_is_written_creating_with_its_token_and_due_at_once(table):
    assert write_run_row(table, call())
    row = run_item(table)
    assert (row["status"], row["run_token"], row["run_version"]) == ("creating", "token-1", 1)
    assert (row["start_day"], row["started_at"], row["attention_at"]) == ("2026-11-19", START, "2026-11-19T06:00:00.000Z")


def test_a_later_attempt_replaces_the_token_and_clears_the_answer_and_an_earlier_one_cannot(table):
    assert write_run_row(table, call(retry_count=2, token="token-3"))
    table.update_item(Key={"execution_name": run_row.key(EXECUTION)}, UpdateExpression="SET answered_order = :o",
                      ExpressionAttributeValues={":o": 2})
    assert write_run_row(table, call(redrive_count=1, retry_count=0, token="token-r"))
    assert not write_run_row(table, call(retry_count=1, token="token-2"))
    row = run_item(table)
    assert row["run_token"] == "token-r" and "answered_order" not in row and row["run_version"] == 2


def test_the_run_row_never_goes_back_to_creating(table):
    write_run_row(table, call())
    table.update_item(**start_run.activation(call(), now=NOW))
    assert write_run_row(table, call(redrive_count=1, token="token-r"))
    assert run_item(table)["status"] == "active"


@pytest.mark.parametrize("existing,accepted", [
    ({"run_execution": EXECUTION, "run_start": START, "step_failure": {"step": "notify"}}, True),  # a step's mark
    ({"run_execution": "orders-run-2026-11-19-ffffffff", "run_start": START}, False),           # another run
    ({"run_execution": EXECUTION, "run_start": "2026-11-19T07:00:00Z"}, False),                 # same name, later
    ({"run_execution": EXECUTION, "run_start": START, "stop": {"by": "engine"}}, False),         # stopped
])
def test_the_run_row_accepts_only_its_own_run_and_no_stopped_one(table, existing, accepted):
    table.put_item(Item={"execution_name": run_row.key(EXECUTION), **existing})
    assert write_run_row(table, call()) is accepted


def test_a_task_row_already_there_is_accepted_only_from_this_run(table):
    from botocore.exceptions import ClientError

    item = items_by_task()["load"]
    table.put_item(Item=item, **start_run.task_condition())
    with pytest.raises(ClientError) as refused:
        table.put_item(Item=item, **start_run.task_condition())
    old = refused.value.response.get("Item")
    from boto3.dynamodb.types import TypeDeserializer
    existing = {k: TypeDeserializer().deserialize(v) for k, v in old.items()}
    start_run.accept_existing(existing, call(), "load's row")                         # a retry of this run
    with pytest.raises(start_run.OtherRun, match="belongs to another run"):
        start_run.accept_existing({**existing, "run_start": "2026-11-19T07:00:00.000Z"}, call(), "load's row")


def test_activation_marks_the_run_active_once_and_never_a_stopped_one(table):
    from botocore.exceptions import ClientError

    write_run_row(table, call())
    table.update_item(**start_run.activation(call(), now=NOW + timedelta(seconds=3)))
    assert run_item(table)["status"] == "active" and run_item(table)["run_version"] == 2
    assert run_item(table)["ttl"] == retention.ttl(NOW + timedelta(seconds=3), retention.HISTORY)   # renewed (§5)
    with pytest.raises(ClientError):
        table.update_item(**start_run.activation(call(), now=NOW))                              # a retry: already active
    assert start_run.activation_refused(run_item(table), call()) is True
    table.update_item(Key={"execution_name": run_row.key(EXECUTION)}, UpdateExpression="SET #s = :c, #stop = :m",
                      ExpressionAttributeNames={"#s": "status", "#stop": "stop"},
                      ExpressionAttributeValues={":c": "creating", ":m": {"by": "engine"}})
    with pytest.raises(ClientError):
        table.update_item(**start_run.activation(call(), now=NOW))
    assert start_run.activation_refused(run_item(table), call()) is False
    with pytest.raises(start_run.OtherRun):
        start_run.activation_refused({**run_item(table), "run_execution": "another"}, call())
    assert start_run.is_this_run(run_item(table), call()) and run_item(table)["run_start"] == START


def test_the_rows_key_takes_the_runs_last_20_characters_without_dots_and_colons():
    assert task_row.short("orders-run-2026-11-19.123:45") == "un-2026-11-1912345"
    assert task_row.key("load", "2026-11-19", "orders-run-2026-11-19.123:45") == "load-2026-11-19-un-2026-11-1912345"


def test_a_retry_a_day_later_still_names_its_rows_by_the_runs_date():
    late = call(retry_count=3, input={})
    rows = start_run.task_items(late, {"extract": SPECS["extract"]}, now=NOW + timedelta(days=1))
    assert rows[0]["date"] == "2026-11-18" and rows[0]["execution_name"].startswith("extract-2026-11-18-")


def test_a_backfill_runs_rows_name_their_backfill():
    rows = start_run.task_items(call(input={"backfill_id": "bf-2026-11-18"}), {"extract": SPECS["extract"]}, now=NOW)
    assert rows[0]["backfill_id"] == "bf-2026-11-18"
    assert "backfill_id" not in start_run.task_items(call(input={}), {"extract": SPECS["extract"]}, now=NOW)[0]


# ── Only an execution of the pipeline's registered state machine (PLAN_IAM 1.4.0 §6.9) ──────

MACHINE = "arn:aws:states:us-east-1:123456789012:stateMachine:orders"


def test_an_execution_of_the_registered_machine_passes():
    start_run.check_execution(call(), MACHINE)


@pytest.mark.parametrize("registered, problem", [
    (None, "the registry names no state machine of this pipeline"),
    ("", "the registry names no state machine of this pipeline"),
    ("arn:aws:lambda:us-east-1:123456789012:function:orders", "the registry names no state machine of this pipeline"),
    ("arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris-bulk-backfill",
     "is not an execution of the pipeline's state machine"),
])
def test_an_execution_the_registry_does_not_vouch_for_is_refused(registered, problem):
    with pytest.raises(start_run.ForeignExecution, match=problem):
        start_run.check_execution(call(), registered)


def test_another_execution_of_the_same_machine_is_refused():
    """The run row names the execution the engine stops and answers: its name must be this run's too."""
    forged = call(execution_arn=f"{MACHINE.replace(':stateMachine:', ':execution:')}:another-run")
    with pytest.raises(start_run.ForeignExecution):
        start_run.check_execution(forged, MACHINE)
