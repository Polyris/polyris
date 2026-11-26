"""The console's Run (`POST /api/pipeline-run`) starts only a pipeline the registry holds.

For a name the registry did not hold, the route listed every state machine of the account and
started the one whose name was closest, and its 404 listed them all: a console user could start
`run_task`, `bulk_backfill` or any state machine of the account (IAM_HOLES_VERIFIED, claim 6c).
The registry is the one source now; the tables are moto and Step Functions is the faked boundary
(Principle #14).
"""
import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from tests.runs_kit import open_registry

SFN_ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris-orders"


@pytest.fixture
def sfn(mocker):
    import config
    from routes import pipelines_actions

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        mocker.patch.object(config, "_dynamodb", resource)
        sfn = mocker.patch.object(pipelines_actions, "sfn")
        sfn.exceptions.ExecutionAlreadyExists = type("ExecutionAlreadyExists", (Exception,), {})
        sfn.start_execution.return_value = {"executionArn": f"{SFN_ARN}:run-1",
                                            "startDate": datetime(2026, 11, 26, tzinfo=timezone.utc)}
        sfn.resource = resource
        yield sfn


def run(name: str):
    from routes import pipelines_actions

    response = pipelines_actions.run_pipeline(name, {"body": json.dumps({"input": {"current_date": "2026-11-26"}})})
    return response["statusCode"], json.loads(response["body"])


def test_a_registered_pipeline_starts_its_registered_state_machine(mocker, sfn):
    open_registry(mocker, sfn.resource, {"pipeline_name": "orders", "sfn_arn": SFN_ARN})
    status, body = run("orders")
    assert (status, body["execution_arn"]) == (200, f"{SFN_ARN}:run-1")
    assert sfn.start_execution.call_args.kwargs["stateMachineArn"] == SFN_ARN


def test_a_pipeline_the_registry_does_not_hold_answers_404_and_starts_nothing(mocker, sfn):
    open_registry(mocker, sfn.resource, {"pipeline_name": "orders", "sfn_arn": SFN_ARN})
    status, body = run("dep-run-task-helper")
    assert status == 404
    assert body == {"error": "Pipeline 'dep-run-task-helper' is not registered on this platform. A pipeline "
                             "registers within a minute of polyris-deploy; deploy it, then run it."}
    assert sfn.method_calls == [], "no state machine is looked up by its name, listed or started"


def test_a_registry_that_cannot_be_read_answers_500(mocker, sfn):
    import importlib

    # The registry table was never created: DynamoDB answers ResourceNotFoundException.
    mocker.patch.object(importlib.import_module("dal.pipelines_repo").pipelines_repo, "_table_name", "no-such-table")
    status, body = run("orders")
    assert status == 500 and "registry" in body["error"]
    assert sfn.method_calls == []
