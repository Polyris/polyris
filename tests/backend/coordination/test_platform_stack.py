"""The real stack loads into the harness, and a generated pipeline starts its run through Start_Run."""

import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from bindings import Bindings
from platform_stack import Stack
from sfn_runtime import Runtime

NOW = "2026-11-12T06:00:00.000Z"
RUN = "orders_daily-run-2026-11-12-0000abcd"


@pytest.fixture
def stack():
    with mock_aws():
        stack = Stack(boto3.client("dynamodb", region_name="us-east-1"), now=lambda: NOW)
        bindings = Bindings(stack, boto3.resource("dynamodb", region_name="us-east-1"),
                            lambda: datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc))
        bindings.start_run()
        yield stack
        bindings.close()


def _one_task_dag():
    from polyris import DAG, task

    with DAG("orders_daily", schedule="cron(0 6 * * ? *)") as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:extract")
        def extract(): ...

        extract()
    return dag


def test_every_state_machine_with_a_definition_file_loads(stack):
    expected = {logical for logical, r in stack.resources.items() if "DefinitionUri" in r.get("Properties", {})}
    assert {stack.machine_arn(logical) for logical in expected} == set(stack.runtime.machines)
    assert set(stack.tables) >= {"PipelineTokensTable", "AssetEventsTable", "DependencySubscriptionsTable"}


def _item(stack, key):
    return stack.ddb.get_item(TableName=stack.tables["PipelineTokensTable"], Key={"execution_name": {"S": key}},
                              ConsistentRead=True).get("Item")


def _start(stack, dag):
    rt = stack.runtime
    pipeline = stack.deploy_pipeline(dag)
    arn = rt.start_execution(stateMachineArn=pipeline, name=RUN,
                             input=json.dumps({"current_date": "2026-11-12"}))["executionArn"]
    rt.run()
    return rt.executions[arn]


def _dag_with_a_failing_step():
    from polyris import DynamoDBTask

    dag = _one_task_dag()
    dag.add_step(DynamoDBTask(step_id="audit", operation="put_item", table_name="no-such-table",
                              item={"pk": {"S": "orders"}}))
    return dag


def test_a_run_writes_its_rows_through_start_run_and_ends_with_the_runs_answer(stack):
    from polyris.coordination import run_row

    execution = _start(stack, _one_task_dag())
    run = _item(stack, run_row.key(RUN))
    assert (run["status"]["S"], run["run_start"]["S"]) == ("active", execution.started)
    assert _item(stack, f"extract-2026-11-12-{RUN[-20:]}")["status"]["S"] == "waiting"
    assert execution.status == "RUNNING"                  # Start_Run waits for the run's token

    stack.runtime.send_task_success(taskToken=run["run_token"]["S"], output="{}")
    stack.runtime.run()
    assert execution.status == "SUCCEEDED"


def test_a_failed_runs_answer_fails_the_execution_without_a_warning(stack):
    from polyris.coordination import run_row

    execution = _start(stack, _one_task_dag())
    token = _item(stack, run_row.key(RUN))["run_token"]["S"]
    stack.runtime.send_task_failure(taskToken=token, error=run_row.FAILED_RUN_ERROR, cause="extract failed")
    stack.runtime.run()
    assert (execution.status, execution.error) == ("FAILED", run_row.FAILED_RUN_ERROR)
    assert "extract failed" in execution.cause
    warnings = stack.ddb.scan(TableName=stack.tables["PipelineTokensTable"])["Items"]
    assert not [w for w in warnings if w["execution_name"]["S"].startswith("_notify_warn_")]


def test_a_failed_direct_step_is_recorded_and_the_run_fails_once_its_rows_are_final(stack):
    from polyris.coordination import run_row

    execution = _start(stack, _dag_with_a_failing_step())
    run = _item(stack, run_row.key(RUN))
    assert run["step_failure"]["M"]["step"]["S"] == "audit"
    warning = _item(stack, f"_notify_warn_step_{RUN}-step-audit")
    assert warning["kind"]["S"] == "step"
    assert "The direct step audit failed: DynamoDB.ResourceNotFoundException" in warning["error"]["S"]
    assert execution.status == "RUNNING"                  # the run's tasks still go on

    stack.runtime.send_task_success(taskToken=run["run_token"]["S"], output="{}")
    stack.runtime.run()
    assert (execution.status, execution.error) == ("FAILED", "DynamoDB.ResourceNotFoundException")


def test_a_start_run_that_fails_for_good_starts_no_task_and_says_why(stack):
    from polyris.coordination import run_row

    pipeline = stack.deploy_pipeline(_one_task_dag())
    for item in stack.ddb.scan(TableName=stack.tables["PipelineSpecsTable"])["Items"]:
        stack.ddb.delete_item(TableName=stack.tables["PipelineSpecsTable"],
                              Key={"pk": item["pk"], "sk": item["sk"]})          # the specs are gone
    arn = stack.runtime.start_execution(stateMachineArn=pipeline, name=RUN,
                                        input=json.dumps({"current_date": "2026-11-12"}))["executionArn"]
    stack.runtime.run()
    execution = stack.runtime.executions[arn]
    assert (execution.status, execution.error) == ("FAILED", "SpecError")
    assert _item(stack, run_row.key(RUN)) is None and _item(stack, f"extract-2026-11-12-{RUN[-20:]}") is None
    assert _item(stack, f"_notify_warn_coordination_{RUN}-start_run")["kind"]["S"] == "coordination"


def test_a_definition_using_an_undefined_substitution_is_refused(tmp_path):
    bad = tmp_path / "sfn.tpl.json"
    bad.write_text('{"StartAt": "A", "States": {"A": {"Type": "Pass", "Output": "${nope}", "End": true}}}')
    with mock_aws():
        stack = Stack(boto3.client("dynamodb", region_name="us-east-1"), now=lambda: NOW)
        stack.resources["Broken"] = {"Type": "AWS::Serverless::StateMachine",
                                     "Properties": {"DefinitionUri": str(bad), "DefinitionSubstitutions": {}}}
        with pytest.raises(AssertionError, match="nope"):
            stack._definition("Broken")


def test_unbound_lambdas_fail_the_state_that_calls_them():
    rt = Runtime(None, now=lambda: NOW)
    m = rt.add_machine("m", {"StartAt": "A", "States": {"A": {
        "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
        "Arguments": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:old-lambda", "Payload": {}}, "End": True}}})
    arn = rt.start_execution(stateMachineArn=m.arn)["executionArn"]
    rt.run()
    assert rt.executions[arn].error == "Lambda.ResourceNotFoundException"
