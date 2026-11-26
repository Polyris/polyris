"""Start_Run's Lambda (ADR #137 §1): its writes, in order, against DynamoDB (moto) as the template declares it."""

import pytest

import env
import failures
import start_run as lambda_start_run
from coordination_testkit import TABLES, client_error, register
from polyris.coordination import run_row, spec_store, start_run, task_row, task_spec

EXECUTION = "orders-run-2026-11-19-abcd1234"
EXECUTION_ARN = f"arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-orders:{EXECUTION}"
START = "2026-11-19T06:00:00.123Z"
SPECS = [
    {"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"},
    {"task_id": "load", "task_type": "sfn", "dependencies": ["extract"], "trigger_rule": "all_success",
     "wait_for": [{"asset_name": "s3/orders"}]},
]


@pytest.fixture
def stack(ddb, monkeypatch):
    import boto3

    client = boto3.client("dynamodb", region_name="us-east-1")
    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_dynamodb_client", client)
    version = task_spec.build_version("orders", SPECS)
    spec_store.write(client, TABLES["SPECS_TABLE"][1], version, sleep=lambda _: None)
    register(ddb, "orders", EXECUTION_ARN)
    return ddb, version


def event(version, **changes):
    payload = {"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id, "task_count": version.task_count,
               "pipeline_name": "orders", "execution_name": EXECUTION, "execution_arn": EXECUTION_ARN, "execution_start": START, "token": "token-1",
               "retry_count": 0, "redrive_count": 0, "date": "2026-11-18", "input": {"triggered_by": "schedule"}}
    payload.update(changes)
    return payload


def tokens(ddb):
    return ddb.Table(TABLES["TOKENS_TABLE"][1])


def run_item(ddb):
    return tokens(ddb).get_item(Key={"execution_name": run_row.key(EXECUTION)})["Item"]


def test_a_run_gets_its_rows_and_its_pointers_and_becomes_active(stack):
    ddb, version = stack
    assert lambda_start_run.handler(event(version), None) == {"run": EXECUTION, "started": True, "tasks": 2}
    assert (run_item(ddb)["status"], run_item(ddb)["run_token"]) == ("active", "token-1")
    load = tokens(ddb).get_item(Key={"execution_name": task_row.key("load", "2026-11-18", EXECUTION)})["Item"]
    assert (load["status"], load["run_start"]) == ("waiting", START)
    pointers = ddb.Table(TABLES["SUBSCRIPTIONS_TABLE"][1]).scan()["Items"]
    assert pointers == [{"dependency_key": "asset:s3/orders", "subscriber": load["execution_name"],
                         "run": run_row.key(EXECUTION), "ttl": load["ttl"]}]     # the run's history TTL (§5)
    assert tokens(ddb).get_item(Key={"execution_name": run_row.input_key(EXECUTION)})["Item"]["run_start"] == START


def test_a_retry_and_a_redrive_write_nothing_twice(stack):
    ddb, version = stack
    lambda_start_run.handler(event(version), None)
    assert lambda_start_run.handler(event(version, retry_count=1, token="token-2"), None)["started"] is True
    assert lambda_start_run.handler(event(version, redrive_count=1, token="token-r"), None)["started"] is True
    assert (run_item(ddb)["status"], run_item(ddb)["run_token"]) == ("active", "token-r")


def test_an_earlier_attempt_that_arrives_late_starts_nothing(stack):
    ddb, version = stack
    lambda_start_run.handler(event(version, retry_count=2, token="token-3"), None)
    assert lambda_start_run.handler(event(version, retry_count=1, token="token-2"), None)["started"] is False
    assert run_item(ddb)["run_token"] == "token-3"


def test_a_run_the_engine_stopped_before_it_became_active_starts_nothing(stack):
    ddb, version = stack
    tokens(ddb).put_item(Item={"execution_name": run_row.key(EXECUTION), "status": "creating", "run_execution": EXECUTION,
                               "run_start": START, "stop": {"by": "engine", "reason": "never became active"}})
    with pytest.raises(lambda_start_run.Stopped, match="stopped before it became active"):
        lambda_start_run.handler(event(version), None)


def test_a_row_of_another_run_under_this_runs_name_fails_the_start(stack):
    ddb, version = stack
    tokens(ddb).put_item(Item={"execution_name": run_row.key(EXECUTION), "run_execution": EXECUTION,
                               "run_start": "2026-11-12T06:00:00.000Z", "status": "active"})
    with pytest.raises(start_run.OtherRun, match="belongs to another run"):
        lambda_start_run.handler(event(version), None)


def test_a_pipeline_of_another_format_is_refused_before_any_aws_call(stack, monkeypatch):
    _, version = stack
    monkeypatch.setattr(env, "_dynamodb", None)            # any AWS call would fail differently
    monkeypatch.setattr(env, "_dynamodb_client", None)
    with pytest.raises(start_run.FormatError, match="redeploy"):
        lambda_start_run.handler(event(version, format_version=0), None)


def test_a_spec_version_that_is_not_there_fails_the_start_with_the_cause(stack):
    _, version = stack
    with pytest.raises(task_spec.SpecError, match="expected 2"):
        lambda_start_run.handler(event(version, spec_id="orders#" + "0" * 64), None)


def test_a_throttled_write_is_transient_so_the_states_retry_repeats_it(stack):
    ddb, version = stack

    def throttle(**_):
        raise client_error("ProvisionedThroughputExceededException", "PutItem")

    ddb.meta.client.meta.events.register("before-call.dynamodb.PutItem", throttle)
    with pytest.raises(failures.CoordinationTransient):
        lambda_start_run.handler(event(version), None)


# ── from the review of TR-4 ─────────────────────────────────────────────────

def test_a_retry_after_the_activation_records_its_token_and_writes_no_row_or_pointer_again(stack):
    """A pointer of a row past its wait must not come back: nothing would delete it (Rule 40)."""
    ddb, version = stack
    lambda_start_run.handler(event(version), None)
    pointers = ddb.Table(TABLES["SUBSCRIPTIONS_TABLE"][1])
    for pointer in pointers.scan()["Items"]:                       # the dispatcher deleted them: load left its wait
        pointers.delete_item(Key={"dependency_key": pointer["dependency_key"], "subscriber": pointer["subscriber"]})
    answer = lambda_start_run.handler(event(version, token="token-2", retry_count=1), None)
    assert answer["started"] is True and run_item(ddb)["run_token"] == "token-2"
    assert pointers.scan()["Items"] == []


def test_a_skip_list_naming_no_task_is_refused_before_any_write(stack):
    ddb, version = stack
    with pytest.raises(ValueError, match="skip_tasks names tasks the pipeline does not have"):
        lambda_start_run.handler(event(version, input={"skip_tasks": ["no_such_task"]}), None)
    assert "Item" not in tokens(ddb).get_item(Key={"execution_name": run_row.key(EXECUTION)})
    assert "Item" not in tokens(ddb).get_item(Key={"execution_name": run_row.input_key(EXECUTION)})


def test_specs_dynamodb_leaves_unread_are_transient_so_the_states_retry_repeats_the_start(stack, monkeypatch):
    """ADR #137 §8: throttling never counts as a missing spec."""
    _, version = stack
    client = env._dynamodb_client

    def throttled(**kwargs):                                       # every key comes back unprocessed
        return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}

    monkeypatch.setattr(client, "batch_get_item", throttled)
    monkeypatch.setattr(spec_store, "_backoff", lambda attempt: 0.0)       # the waits between rounds, not the rounds
    with pytest.raises(failures.CoordinationTransient):
        lambda_start_run.handler(event(version), None)


# ── A forged payload (PLAN_IAM 1.4.0 §6.9) ──────────────────────────────────


@pytest.mark.parametrize("execution_arn", [
    f"arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-bulk-backfill:{EXECUTION}",
    f"arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-orders:another-run",
    f"arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris-orders:{EXECUTION}",
])
def test_an_execution_that_is_not_one_of_the_pipelines_registered_machine_gets_no_row(stack, execution_arn):
    """The engine stops and answers the execution the run row names: a forged one must never get there."""
    ddb, version = stack
    with pytest.raises(start_run.ForeignExecution, match="is not an execution of the pipeline's state machine"):
        lambda_start_run.handler(event(version, execution_arn=execution_arn), None)
    assert "Item" not in tokens(ddb).get_item(Key={"execution_name": run_row.key(EXECUTION)})


def test_a_pipeline_the_registry_names_no_machine_of_gets_no_row(stack):
    ddb, version = stack
    with pytest.raises(start_run.ForeignExecution, match="the registry names no state machine of this pipeline"):
        lambda_start_run.handler(event(version, pipeline_name="unregistered"), None)
    assert "Item" not in tokens(ddb).get_item(Key={"execution_name": run_row.key(EXECUTION)})
