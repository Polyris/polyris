"""The real stack loads into the harness and runs a pipeline through the real templates."""

import json

import boto3
import pytest
from moto import mock_aws

from platform_stack import Stack
from sfn_runtime import Runtime

NOW = "2026-11-12T06:00:00.000Z"
RUN = "orders_daily-run-2026-11-12-0000abcd"


@pytest.fixture
def stack():
    with mock_aws():
        yield Stack(boto3.client("dynamodb", region_name="us-east-1"), now=lambda: NOW)


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


def test_a_root_task_runs_to_success_through_the_real_templates(stack):
    rt = stack.runtime
    pipeline = stack.deploy_pipeline(_one_task_dag())
    rt.start_execution(stateMachineArn=pipeline, name=RUN, input=json.dumps({"current_date": "2026-11-12"}))
    rt.run()

    [job] = rt.jobs.values()     # the user's state machine, started by run_task
    assert job.arguments["StateMachineArn"].endswith(":stateMachine:extract")
    rt.finish_job(job.id, output={"Output": {}, "Status": "SUCCEEDED"})
    rt.run()

    row = stack.ddb.get_item(TableName=stack.tables["PipelineTokensTable"],
                             Key={"execution_name": {"S": f"extract-2026-11-12-{RUN[-20:]}"}})["Item"]
    assert row["status"]["S"] == "success"
    assert rt.executions[f"{pipeline.replace(':stateMachine:', ':execution:')}:{RUN}"].status == "SUCCEEDED"


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
