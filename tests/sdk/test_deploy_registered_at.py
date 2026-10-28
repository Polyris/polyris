"""polyris-deploy owns `registered_at` on pipeline-registry.

Before this change, every pipeline run would overwrite `registered_at` with the
run start time, so the UI's "Deployed at X" was really "Last run at X". Fix is
twofold: (1) the generated pipeline SFN's Register_Pipeline state no longer
writes registered_at — asserted in test_register_pipeline_omits_registered_at;
(2) polyris-deploy writes it directly via UpdateItem on pipeline-registry —
asserted here. Any regression on either half re-opens the bug.
"""
import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import BotoCoreError, ClientError

from polyris import DAG, deploy, task
from polyris.deploy import deploy_pipeline
from polyris.generators import generate_step_function_json

_ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:acme-dev-polyris-my-pipeline"
_SAM_OUTPUTS = [
    {"OutputKey": "DependencyWrapperArn", "OutputValue": _ARN},
    {"OutputKey": "OrchestrationRoleArn", "OutputValue": "arn:aws:iam::000000000000:role/exec"},
    {"OutputKey": "PipelineRegistryTable", "OutputValue": "my-registry"},
    {"OutputKey": "PipelineTokensTable", "OutputValue": "tokens"},
    {"OutputKey": "AssetSubscriptionsTable", "OutputValue": "asset-subs"},
    {"OutputKey": "ResultsBucket", "OutputValue": "results-bucket"},
]
_PIPELINE_OUTPUTS = [{"OutputKey": "StateMachineArn", "OutputValue": _ARN}]


def _make_deploy_mocks(mocker):
    """Wire enough boto3 + config mocks for deploy_pipeline to reach the
    registration section without hitting real AWS."""
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    mocker.patch("polyris.deploy.subprocess.run", return_value=mocker.MagicMock(returncode=0))
    boto = mocker.patch("polyris.deploy.boto3")
    session = mocker.MagicMock()
    boto.Session.return_value = session
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "000000000000"}
    cfn = mocker.MagicMock()

    def _describe_stacks(StackName):
        outs = _SAM_OUTPUTS if StackName == "acme-dev" else _PIPELINE_OUTPUTS
        return {"Stacks": [{"Outputs": outs, "Parameters": [
            {"ParameterKey": "Stage", "ParameterValue": "dev"},
            {"ParameterKey": "Namespace", "ParameterValue": "acme"},
        ]}]}

    cfn.describe_stacks.side_effect = _describe_stacks
    scheduler = mocker.MagicMock()
    scheduler.get_schedule.return_value = {
        "GroupName": "default",
        "ScheduleExpression": "cron(0 12 * * ? *)",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": {"Arn": _ARN, "RoleArn": "x"},
        "State": "ENABLED",
    }

    def _client(name):
        return {"sts": sts, "cloudformation": cfn, "scheduler": scheduler}.get(
            name, mocker.MagicMock())

    session.client.side_effect = _client

    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    cfg.region = "us-east-1"
    cfg.namespace = "acme"
    cfg.profile = None
    stage_cfg = mocker.MagicMock()
    stage_cfg.stack_name = "acme-dev"
    stage_cfg.namespace = "acme"
    stage_cfg.region = "us-east-1"
    stage_cfg.profile = None
    stage_cfg.get = lambda k, default=None: {"account_id": "000000000000"}.get(k, default)
    cfg.for_stage.return_value = stage_cfg
    return session


# ---------------------------------------------------------------------------
# Half 1: SFN Register_Pipeline must not touch registered_at
# ---------------------------------------------------------------------------

def test_register_pipeline_state_is_updateitem_not_putitem():
    """putItem replaces the whole row — would wipe the deploy-time stamp. The
    switch to updateItem is what makes polyris-deploy's direct write survive."""
    from polyris import DAG, task

    with DAG("p", schedule="rate(1 day)") as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:1:stateMachine:x")
        def t():
            pass
        t()

    asl = json.loads(generate_step_function_json(dag, registry_table="r"))
    state = asl["States"]["Register_Pipeline"]
    assert state["Resource"] == "arn:aws:states:::dynamodb:updateItem"


def test_register_pipeline_omits_registered_at():
    """The runtime writer must not include registered_at in any form —
    UpdateExpression, ExpressionAttributeValues, or legacy Item/Parameters."""
    from polyris import DAG, task

    with DAG("p", schedule="rate(1 day)") as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:1:stateMachine:x")
        def t():
            pass
        t()

    asl = json.loads(generate_step_function_json(dag, registry_table="r"))
    args = asl["States"]["Register_Pipeline"]["Arguments"]

    assert "registered_at" not in args.get("UpdateExpression", "")
    assert ":registered_at" not in args.get("ExpressionAttributeValues", {})
    # Defensive: legacy PutItem shapes should be absent entirely.
    assert "Item" not in args
    assert "registered_at" not in json.dumps(args)


def test_register_pipeline_still_writes_other_metadata():
    """The row keeps getting refreshed on every run for everything that IS
    runtime-owned — sfn_arn, tasks, dag, schedule, asset_schedule, etc. If this
    regresses to only writing a subset, the UI's pipeline detail goes stale."""
    from polyris import DAG, task

    with DAG("p", schedule="rate(1 day)", group="g") as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:1:stateMachine:x")
        def t():
            pass
        t()

    asl = json.loads(generate_step_function_json(dag, registry_table="r"))
    args = asl["States"]["Register_Pipeline"]["Arguments"]
    attr_values = args["ExpressionAttributeValues"]

    for field in (
        ":sfn_arn", ":description", ":pipeline_group", ":tasks",
        ":dag", ":schedule", ":asset_schedule", ":last_execution",
    ):
        assert field in attr_values, f"missing {field}"


# ---------------------------------------------------------------------------
# Orphaned helper SFN must also not clobber registered_at
# ---------------------------------------------------------------------------

def test_orphaned_register_pipeline_sfn_uses_updateitem():
    """The helper SFN uses putItem, which replaces the entire row — not just the
    fields listed in Item. Keeping registered_at absent from a putItem does NOT
    protect the field; it DELETES it. The file must use updateItem so wiring it
    up in the future does not silently undo the deploy-stamp contract."""
    import json as _json
    from pathlib import Path

    sfn_path = (
        Path(__file__).parent.parent.parent
        / "sam" / "sfn_templates" / "helpers" / "register_pipeline" / "sfn.tpl.json"
    )
    content = sfn_path.read_text()
    # .tpl.json files contain ${var} substitutions — strip them before parsing.
    import re
    cleaned = re.sub(r'"?\$\{[^}]+\}"?', '"__placeholder__"', content)
    template = _json.loads(cleaned)

    register_state = template["States"]["RegisterPipeline"]
    assert register_state["Resource"] == "arn:aws:states:::dynamodb:updateItem", (
        "RegisterPipeline must use updateItem, not putItem — putItem replaces "
        "the entire row and would delete registered_at"
    )
    assert "registered_at" not in _json.dumps(register_state)


# ---------------------------------------------------------------------------
# Half 2: polyris-deploy writes registered_at via UpdateItem
# ---------------------------------------------------------------------------

def test_stamp_registered_at_writes_updateitem_on_registry(mocker):
    """The authoritative deploy-time write: UpdateItem on the registry table
    keyed by pipeline_name, with a SET for registered_at only."""
    session = mocker.MagicMock()
    ddb = session.client.return_value

    deploy._stamp_registered_at(session, "my-registry", "my-pipeline")

    session.client.assert_called_once_with("dynamodb")
    ddb.update_item.assert_called_once()
    kwargs = ddb.update_item.call_args.kwargs

    assert kwargs["TableName"] == "my-registry"
    assert kwargs["Key"] == {"pipeline_name": {"S": "my-pipeline"}}
    assert kwargs["UpdateExpression"] == "SET registered_at = :now"
    # ISO-8601 UTC — parseable; narrows to "actual timestamp, not a placeholder".
    stamp = kwargs["ExpressionAttributeValues"][":now"]["S"]
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None


def test_stamp_registered_at_surfaces_botocoreerror_as_hard_exit(mocker, capsys):
    """BotoCoreError (network, config, credential) takes the same exit path as
    ClientError — the caller (`deploy.py`) catches `(ClientError, BotoCoreError)`
    and both must produce the red ❌ + sys.exit(1) so neither failure is silent."""
    session = mocker.MagicMock()
    ddb = session.client.return_value

    class _FakeBotoCoreError(BotoCoreError):
        fmt = "connection timeout"

    ddb.update_item.side_effect = _FakeBotoCoreError()

    with pytest.raises(SystemExit) as exc:
        deploy._stamp_registered_at(session, "my-registry", "my-pipeline")

    assert exc.value.code == 1
    err = capsys.readouterr().out
    assert "❌" in err
    assert "my-pipeline" in err
    assert "my-registry" in err


def test_stamp_registered_at_surfaces_clienterror_as_hard_exit(mocker, capsys):
    """Error path uses the same style as the rest of deploy.py — red ❌ line
    naming both the pipeline and the table, then sys.exit(1). A silent swallow
    here would ship a stack with no visible "Deployed at" and no explanation."""
    session = mocker.MagicMock()
    ddb = session.client.return_value
    ddb.update_item.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no table"}},
        "UpdateItem",
    )

    with pytest.raises(SystemExit) as exc:
        deploy._stamp_registered_at(session, "missing-registry", "my-pipeline")

    assert exc.value.code == 1
    err = capsys.readouterr().out
    assert "❌" in err
    assert "my-pipeline" in err
    assert "missing-registry" in err


# ---------------------------------------------------------------------------
# Call-site: deploy_pipeline must call _stamp_registered_at
# ---------------------------------------------------------------------------

def test_deploy_pipeline_calls_stamp_registered_at(mocker):
    """deploy_pipeline must call _stamp_registered_at once, with the registry
    table name from the SAM stack outputs and the dag_id of the deployed DAG.
    If this is never called, registered_at is never written and the Console
    shows no "Deployed at" for the pipeline."""
    _make_deploy_mocks(mocker)

    call_order: list[str] = []
    stamp = mocker.patch(
        "polyris.deploy._stamp_registered_at",
        side_effect=lambda *a, **kw: call_order.append("stamp"),
    )
    mocker.patch(
        "polyris.deploy._register_pipeline",
        side_effect=lambda *a, **kw: call_order.append("register"),
    )

    with DAG("my-pipeline", schedule="rate(1 day)") as dag:
        @task.sfn(arn=_ARN)
        def t():
            pass
        t()

    deploy_pipeline(dag, stage="dev", region="us-east-1")

    stamp.assert_called_once()
    _session_arg, table_arg, dag_id_arg = stamp.call_args.args
    assert table_arg == "my-registry"
    assert dag_id_arg == "my-pipeline"
    # registered_at must be stamped before the registration SFN fires —
    # the SFN's Register_Pipeline state runs asynchronously and must find the
    # row already having a deploy timestamp when it upserts runtime fields.
    assert call_order == ["stamp", "register"]
