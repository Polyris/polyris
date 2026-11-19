"""The generated pipeline runs its tasks through Start_Run (ADR #137 §1).

The JSONata of the generated states is evaluated as Step Functions would
(sfn_jsonata), and what it writes goes through DynamoDB's own conditions (moto),
so the generated side and the Lambda's side of each contract meet here.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from polyris import DAG, Task
from polyris.coordination import run_row, start_run, task_spec
from polyris.generators import BRANCH_ERROR, build_spec_version, generate_step_function_json
from polyris.steps import DynamoDBTask, GlueTask, Pass
from sfn_jsonata import evaluate, find_state, plain

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:user-load"
FUNCTION = "arn:aws:lambda:us-east-1:123456789012:function:acme-dev-polyris-start-run"
RUN = "orders-run-2026-11-19-abcd1234"
START = "2026-11-19T06:00:00.123Z"
CONTEXT = {"Execution": {"Name": RUN, "StartTime": START, "RedriveCount": 0,
                         "Input": {"current_date": "2026-11-18", "backfill_id": "bf-1"}},
           "State": {"RetryCount": 2}, "Task": {"Token": "token-3"}}


def _dag(*, tasks: bool = True) -> DAG:
    dag = DAG("orders")
    if tasks:
        dag.add_task(Task(task_id="extract", arn=ARN))
        dag.add_step(GlueTask(step_id="compact", job_name="compact-orders"))
    dag.add_step(Pass(step_id="mark_done"))
    dag.add_step(DynamoDBTask(step_id="audit", operation="put_item", table_name="audit",
                              item={"pk": {"S": "orders"}}))
    return dag


def _definition(dag: DAG) -> Dict[str, Any]:
    return json.loads(generate_step_function_json(dag, start_run_function_arn=FUNCTION, tokens_table="tokens"))


@pytest.fixture(scope="module")
def states() -> Dict[str, Any]:
    return _definition(_dag())["States"]


def test_tasks_and_wrapper_steps_are_rows_and_each_direct_step_is_a_branch(states):
    branches = states["Run_All_Tasks"]["Branches"]
    assert [b["StartAt"] for b in branches] == ["Start_Run", "mark_done", "audit"]
    assert "startExecution" not in json.dumps(branches)            # no wrapper is started
    assert set(build_spec_version(_dag()).texts) == {"extract", "compact"}   # the Glue step is a row


def test_start_runs_payload_is_what_start_run_reads(states):
    payload = evaluate(states["Run_All_Tasks"]["Branches"][0]["States"]["Start_Run"]["Arguments"]["Payload"],
                       input={"current_date": "2026-11-18", "skip_tasks": ["extract"], "triggered_by": "console",
                              "cascade_all": True},
                       context=CONTEXT)
    call = start_run.call_from(payload)
    version = build_spec_version(_dag())
    assert (call.format_version, call.spec_id, call.task_count) == (task_spec.FORMAT_VERSION, version.spec_id, 2)
    assert (call.execution, call.execution_start, call.token, call.order) == (RUN, START, "token-3", 2)
    assert call.date == "2026-11-18"
    assert set(payload[start_run.Arg.INPUT]) == {v for k, v in vars(start_run.RunArg).items() if not k.startswith("_")}
    assert start_run.skipped(call) == ("extract",) and start_run.triggered_by(call) == "console"
    assert call.execution_input[start_run.RunArg.BACKFILL_ID] == "bf-1"     # from the execution's own input
    assert set(payload) == {v for k, v in vars(start_run.Arg).items() if not k.startswith("_")}


def test_a_first_execution_without_a_redrive_count_orders_its_token_by_its_retries(states):
    payload = states["Run_All_Tasks"]["Branches"][0]["States"]["Start_Run"]["Arguments"]["Payload"]
    context = {**CONTEXT, "Execution": {k: v for k, v in CONTEXT["Execution"].items() if k != "RedriveCount"}}
    assert start_run.call_from(evaluate(payload, input={}, context=context)).order == 2


def test_no_retry_matches_a_failed_runs_answer_which_the_first_catch_takes_without_a_warning(states):
    """TR-0 B4: a Retry that matched the answer would start the run again after its end."""
    state = find_state(states, "Start_Run")
    retried = {error for retrier in state["Retry"] for error in retrier["ErrorEquals"]}
    assert retried == set(start_run.RETRY_ERRORS) and run_row.FAILED_RUN_ERROR not in retried
    first, data_limit, every = state["Catch"]
    assert first["ErrorEquals"] == [run_row.FAILED_RUN_ERROR] and first["Next"] == "Run_Ended_Failed"
    assert data_limit["ErrorEquals"] == ["States.DataLimitExceeded"] and every["ErrorEquals"] == ["States.ALL"]
    assert data_limit["Next"] == every["Next"] == "Warn_Start_Run_Failed"
    recorded = evaluate(first["Output"], error_output={"Error": run_row.FAILED_RUN_ERROR, "Cause": "load failed"})
    assert recorded == {BRANCH_ERROR: {"branch": "Start_Run", "error": run_row.FAILED_RUN_ERROR, "cause": "load failed"}}


def test_the_retry_of_start_run_is_the_window_the_sweeper_waits_for(states):
    [retrier] = find_state(states, "Start_Run")["Retry"]
    assert retrier["MaxAttempts"] == start_run.RETRY_ATTEMPTS and retrier["BackoffRate"] == start_run.RETRY_BACKOFF
    assert retrier["IntervalSeconds"] == start_run.RETRY_INTERVAL.total_seconds()
    assert retrier["MaxDelaySeconds"] == start_run.RETRY_MAX_DELAY.total_seconds()


def test_a_pipeline_without_tasks_has_no_start_run_and_marks_no_run_row():
    states = _definition(_dag(tasks=False))["States"]
    assert [b["StartAt"] for b in states["Run_All_Tasks"]["Branches"]] == ["mark_done", "audit"]
    assert find_state(states, "Warn_Step_2_Failed")["Next"] == "Step_2_Failed"
    with pytest.raises(KeyError):
        find_state(states, "Mark_Step_2_Failed")


@pytest.mark.parametrize("outputs,fails", [
    ([{}, {"x": 1}, "text", None], False),
    ([{}, {BRANCH_ERROR: {"branch": "audit", "error": "DynamoDB.ResourceNotFoundException", "cause": "no table"}}], True),
])
def test_the_execution_fails_after_the_parallel_only_when_a_branch_recorded_an_error(states, outputs, fails):
    [choice] = states["Check_Branches"]["Choices"]
    assert evaluate(choice["Condition"], input=outputs) is fails


def test_the_execution_fails_with_the_first_error_and_every_cause():
    states = _definition(_dag())["States"]
    outputs = [{BRANCH_ERROR: {"branch": "Start_Run", "error": run_row.FAILED_RUN_ERROR, "cause": "extract failed"}},
               {}, {BRANCH_ERROR: {"branch": "audit", "error": "DynamoDB.ResourceNotFoundException", "cause": ""}}]
    assert evaluate(states["Run_Failed"]["Error"], input=outputs) == run_row.FAILED_RUN_ERROR
    assert evaluate(states["Run_Failed"]["Cause"], input=outputs) == (
        f"Start_Run: {run_row.FAILED_RUN_ERROR}: extract failed; audit: DynamoDB.ResourceNotFoundException")
    single = [outputs[2]]
    assert evaluate(states["Run_Failed"]["Error"], input=single) == "DynamoDB.ResourceNotFoundException"


def test_a_wait_or_pass_step_takes_no_catch_so_its_branch_is_the_state_alone(states):
    """AWS rejects Retry and Catch on any state but Task, Parallel and Map."""
    branch = next(b for b in states["Run_All_Tasks"]["Branches"] if b["StartAt"] == "mark_done")
    assert list(branch["States"]) == ["mark_done"] and "Catch" not in branch["States"]["mark_done"]


def test_validation_refuses_a_catch_where_aws_takes_none():
    from polyris.generators import validate_asl

    asl = {"StartAt": "W", "States": {
        "W": {"Type": "Wait", "Seconds": 1, "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "D"}], "Next": "D"},
        "D": {"Type": "Pass", "Retry": [{"ErrorEquals": ["States.ALL"]}], "End": True}}}
    valid, errors, _ = validate_asl(asl)
    assert not valid and errors == ["State 'W': Type 'Wait' does not take 'Catch'",
                                    "State 'D': Type 'Pass' does not take 'Retry'"]


def test_a_failed_direct_step_is_caught_in_its_branch_whatever_the_error(states):
    catchers = find_state(states, "audit")["Catch"]
    assert [c["ErrorEquals"] for c in catchers] == [["States.DataLimitExceeded"], ["States.ALL"]]
    assert {c["Next"] for c in catchers} == {"Warn_Step_2_Failed"}
    assert find_state(states, "Warn_Step_2_Failed")["Next"] == "Mark_Step_2_Failed"
    assert find_state(states, "Mark_Step_2_Failed")["Next"] == "Step_2_Failed"


# ── the step mark and Start_Run's writes, as DynamoDB evaluates them ──

@pytest.fixture
def ddb():
    import boto3
    from moto import mock_aws

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
                            BillingMode="PAY_PER_REQUEST")
        yield client, boto3.resource("dynamodb", region_name="us-east-1").Table("tokens")


def _mark(client, states, *, run: str = RUN, start: str = START) -> bool:
    arguments = evaluate(find_state(states, "Mark_Step_2_Failed")["Arguments"],
                         input={BRANCH_ERROR: {"branch": "audit", "error": "DynamoDB.ResourceNotFoundException",
                                               "cause": "Requested resource not found"}},
                         context={"Execution": {"Name": run, "StartTime": start}})
    try:
        client.update_item(**arguments)
        return True
    except client.exceptions.ConditionalCheckFailedException:
        return False


def _call(**changes: Any) -> start_run.Call:
    payload = {"format_version": task_spec.FORMAT_VERSION, "spec_id": "orders#" + "f" * 64, "task_count": 2,
               "pipeline_name": "orders", "execution_name": RUN, "execution_start": START, "token": "token-1",
               "retry_count": 0, "redrive_count": 0, "date": "2026-11-18", **changes}
    return start_run.call_from(payload)


NOW = datetime(2026, 11, 19, 6, 0, 5, tzinfo=timezone.utc)


def test_a_mark_first_then_start_run_both_stand(ddb, states):
    client, table = ddb
    assert _mark(client, states)
    table.update_item(**start_run.run_row_write(_call(), now=NOW))
    row = table.get_item(Key={"execution_name": run_row.key(RUN)})["Item"]
    assert row[run_row.STEP_FAILURE] == {run_row.STEP_MARK_STEP: "audit",
                                         run_row.STEP_MARK_ERROR: "DynamoDB.ResourceNotFoundException"[:run_row.ERROR_CHARS]}
    assert (row["status"], row["run_token"], row["run_version"]) == ("creating", "token-1", 1)


def test_start_run_first_then_the_mark_keeps_the_run_version_and_the_ttl(ddb, states):
    client, table = ddb
    table.update_item(**start_run.run_row_write(_call(), now=NOW))
    before = table.get_item(Key={"execution_name": run_row.key(RUN)})["Item"]
    assert _mark(client, states)
    after = table.get_item(Key={"execution_name": run_row.key(RUN)})["Item"]
    assert (after["run_version"], after["ttl"]) == (before["run_version"], before["ttl"])
    assert run_row.STEP_FAILURE in after


def test_a_mark_writes_the_history_ttl_on_a_row_without_one(ddb, states):
    client, table = ddb
    _mark(client, states)
    row = table.get_item(Key={"execution_name": run_row.key(RUN)})["Item"]
    from polyris.coordination import retention
    from sfn_jsonata import NOW as JSONATA_NOW
    assert row["ttl"] == retention.ttl(JSONATA_NOW, retention.HISTORY)


def test_a_mark_never_lands_on_another_runs_row(ddb, states):
    client, table = ddb
    table.update_item(**start_run.run_row_write(_call(execution_start="2026-11-12T06:00:00.000Z"), now=NOW))
    assert not _mark(client, states)


def test_a_mark_clips_its_step_to_the_run_rows_cap():
    dag = DAG("orders")
    dag.add_task(Task(task_id="extract", arn=ARN))
    dag.add_step(DynamoDBTask(step_id="s" * 200, operation="put_item", table_name="audit", item={"pk": {"S": "x"}}))
    mark = find_state(_definition(dag)["States"], "Mark_Step_1_Failed")
    step = mark["Arguments"]["ExpressionAttributeValues"][":mark"]["M"][run_row.STEP_MARK_STEP]["S"]
    assert len(step.encode()) == run_row.STEP_MAX


def test_a_generated_mark_is_the_run_rows_step_mark(states):
    """The field names the JSONata writes are run_row's (ADR #137 §9)."""
    arguments = find_state(states, "Mark_Step_2_Failed")["Arguments"]
    assert set(arguments["ExpressionAttributeValues"][":mark"]["M"]) == set(run_row.step_mark(step="a", error="b"))
    assert set(arguments["ExpressionAttributeNames"].values()) == {run_row.STEP_FAILURE, run_row.EXECUTION,
                                                                   run_row.RUN_START, run_row.TTL}
    assert plain({"k": evaluate(arguments["Key"][run_row.TABLE_KEY],
                                context={"Execution": {"Name": RUN}})})["k"] == run_row.key(RUN)
