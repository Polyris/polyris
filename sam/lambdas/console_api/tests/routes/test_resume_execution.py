"""Resume clears the pause, releases pause tokens and has the reconciler check the run (ADR #130 §6.5).

DynamoDB is moto, with the tokens table built from sam/template.yaml. Step
Functions and Lambda are fakes at the client boundary (`config._sfn`,
`config._lambda`), where the console's lazy clients resolve.
The whole flow, with the real reconciler and wrapper, runs in
tests/backend/coordination (B-P1, B-P2).
"""
import importlib.util
import io
import json
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

RUN = "orders-run-2026-11-12-abcdef12"
RECONCILE_ARN = "arn:aws:lambda:us-east-1:123456789012:function:reconcile"


class FakeSfn:
    def __init__(self, dead=()):
        self.released, self.dead = [], set(dead)

    def send_task_success(self, *, taskToken, output):
        if taskToken in self.dead:
            raise ClientError({"Error": {"Code": "TaskTimedOut", "Message": "expired"}}, "SendTaskSuccess")
        self.released.append((taskToken, json.loads(output)))


class FakeLambda:
    def __init__(self, response=None, error=None):
        self.calls, self.response, self.error = [], response, error

    def invoke(self, *, FunctionName, InvocationType, Payload):
        self.calls.append((FunctionName, InvocationType, json.loads(Payload)))
        if self.error:
            raise self.error
        return self.response or {"StatusCode": 200, "Payload": io.BytesIO(b'{"handled": 2, "deferred": 1}')}


@pytest.fixture
def tokens(mocker):
    import config
    from dal import executions_repo

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(executions_repo, "_table_name", name)
        yield resource.Table(name)


def _resume(mocker, sfn, lam, arn=RECONCILE_ARN):
    import config
    import routes.executions as executions

    mocker.patch.object(config, "_sfn", sfn)
    mocker.patch.object(config, "_lambda", lam)
    mocker.patch.object(executions, "RECONCILE_FUNCTION_ARN", arn)
    response = executions.resume_execution(RUN, {})
    return response["statusCode"], json.loads(response["body"])


def _task(table, name, **fields):
    table.put_item(Item={"execution_name": name, "task_name": name.split("-")[0], "pipeline_execution": RUN, **fields})


def test_resume_clears_the_pause_releases_pause_tokens_and_reconciles_the_run(tokens, mocker):
    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": True, "pipeline_execution": RUN})
    _task(tokens, "load-1", status="waiting_paused", pause_token="pause-1")
    _task(tokens, "report-1", status="waiting_paused", wait_token="wait-1")      # parked by the reconciler
    sfn, lam = FakeSfn(), FakeLambda()

    status, body = _resume(mocker, sfn, lam)

    assert status == 200
    assert tokens.get_item(Key={"execution_name": f"_pause_{RUN}"})["Item"]["paused"] is False
    assert sfn.released == [("pause-1", {"resumed": True})]                   # never the wait_token
    assert lam.calls == [(RECONCILE_ARN, "RequestResponse", {"pipeline_execution": RUN, "source": "resume"})]
    assert body["resumed_tasks"] == ["load"] and body["reconcile"] == {"handled": 2, "deferred": 1}
    # run_task writes `running` and drops the token itself: resume writes no task status.
    assert tokens.get_item(Key={"execution_name": "load-1"})["Item"]["status"] == "waiting_paused"


def test_a_dead_pause_token_is_reported_and_the_rest_still_resume(tokens, mocker):
    _task(tokens, "load-1", status="waiting_paused", pause_token="pause-1")
    _task(tokens, "load2-1", status="waiting_paused", pause_token="pause-2")
    sfn = FakeSfn(dead={"pause-1"})

    status, body = _resume(mocker, sfn, FakeLambda())

    assert status == 200
    assert body["failed_tasks"] == ["load (TaskTimedOut)"]
    assert body["resumed_tasks"] == ["load2"]


@pytest.mark.parametrize("lam,arn,error", [
    (FakeLambda(response={"StatusCode": 200, "FunctionError": "Unhandled",
                          "Payload": io.BytesIO(b'{"errorMessage": "CoordinationTransient: throttled"}')}),
     RECONCILE_ARN, "CoordinationTransient: throttled"),
    (FakeLambda(error=ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "slow down"}}, "Invoke")),
     RECONCILE_ARN, "TooManyRequestsException"),
    (FakeLambda(), "", "RECONCILE_FUNCTION_ARN is not set"),
])
def test_a_failed_reconcile_still_resumes_and_says_why(tokens, mocker, lam, arn, error):
    """The run's waiting tasks keep their attention entries, so the sweeper checks them."""
    status, body = _resume(mocker, FakeSfn(), lam, arn=arn)

    assert status == 200
    assert error in body["reconcile"]["error"]
