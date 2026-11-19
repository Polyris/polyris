"""polyris-deploy writes the task specs, reads them back and checks them, and only then
creates the change set (ADR #137 §8).

Mocked at the AWS boundary: CloudFormation, STS and the scheduler are fakes of the
boto3 clients; DynamoDB is moto. A fault injected between the write and the read
back (a version item or a text dropped or altered) must stop the deploy before
``create_change_set``.
"""

import json
import re
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from polyris import DAG, deploy, task
from polyris.coordination import spec_store, task_spec
from polyris.coordination.task_spec import PK, SK, TEXT, VERSION_HASH
from polyris.deploy import deploy_pipeline
from polyris.generators import build_spec_version

SPECS = "acme-dev-polyris-pipeline-specs"  # conftest.SPECS_TABLE
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:acme-dev-polyris-orders"
IAM_DOC = Path(__file__).resolve().parents[2] / "docs" / "deployment" / "IAM_PERMISSIONS.md"


def _dag():
    with DAG("orders", schedule=None) as dag:
        @task.sfn(arn=ARN)
        def extract():
            pass

        @task.glue_job(job_name="load")
        def load():
            pass
        extract() >> load()
    return dag


def _platform(mocker, ddb, *, outputs=None):
    """Fakes of every AWS client but DynamoDB; returns the CloudFormation fake."""
    mocker.patch("polyris.validation.validate_asl_from_dag", return_value=(True, [], []))
    mocker.patch("polyris.deploy._register_pipeline")
    sam_outputs = outputs if outputs is not None else {
        "DependencyWrapperArn": ARN, "OrchestrationRoleArn": "arn:aws:iam::000000000000:role/exec",
        "PipelineRegistryTable": "registry", "PipelineTokensTable": "tokens",
        "AssetSubscriptionsTable": "asset-subs", "PipelineSpecsTable": SPECS, "ResultsBucket": "results"}
    cfn = mocker.MagicMock()

    def describe_stacks(StackName):
        outs = sam_outputs if StackName == "acme-dev" else {"StateMachineArn": ARN}
        return {"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in outs.items()],
                            "Parameters": []}]}
    cfn.describe_stacks.side_effect = describe_stacks
    cfn.describe_change_set.return_value = {"Status": "CREATE_COMPLETE", "ChangeSetName": "cs"}
    sts = mocker.MagicMock()
    sts.get_caller_identity.return_value = {"Account": "000000000000"}
    session = mocker.MagicMock()
    session.client.side_effect = lambda name, **_: {"sts": sts, "cloudformation": cfn, "dynamodb": ddb}.get(
        name, mocker.MagicMock())
    mocker.patch("polyris.deploy.boto3").Session.return_value = session
    cfg = mocker.patch("polyris.deploy.polyris_config")
    cfg.stage = "dev"
    stage = mocker.MagicMock(stack_name="acme-dev", namespace="acme", region="us-east-1", profile=None)
    stage.get = lambda key, default=None: default
    cfg.for_stage.return_value = stage
    mocker.patch("polyris.config._find_project_config", return_value="config.py")
    return cfn


class _Faulty:
    """The moto client, with a fault injected after the spec write and before the read back."""

    def __init__(self, client, fault):
        self.client, self.fault, self.done = client, fault, False

    def __getattr__(self, name):
        if name == "query" and not self.done:
            self.done = True
            self.fault(self.client)
        return getattr(self.client, name)


def test_a_deploy_writes_the_specs_its_definition_names_before_the_change_set(mocker, platform_dynamodb):
    order = []
    ddb = mocker.MagicMock(wraps=platform_dynamodb)
    ddb.batch_write_item.side_effect = lambda **kw: (order.append("specs"),
                                                     platform_dynamodb.batch_write_item(**kw))[1]
    cfn = _platform(mocker, ddb)
    cfn.create_change_set.side_effect = lambda **kw: (order.append("change set"), {"Id": "cs"})[1]
    deploy_pipeline(_dag(), stage="dev", region="us-east-1")

    version = build_spec_version(_dag())
    assert order[0] == "specs" and "change set" in order
    template = json.loads(cfn.create_change_set.call_args.kwargs["TemplateBody"])
    definition = template["Resources"]["PipelineStateMachine"]["Properties"]["DefinitionString"]
    assert version.spec_id in definition
    assert spec_store.read(platform_dynamodb, SPECS, version.spec_id, expected_count=2).keys() == {"extract", "load"}
    registry = platform_dynamodb.get_item(TableName="registry", Key={"pipeline_name": {"S": "orders"}})["Item"]
    assert registry[task_spec.REGISTRY_FORMAT_FIELD] == {"N": str(task_spec.FORMAT_VERSION)}


def _drop_version_item(client):
    version = build_spec_version(_dag())
    client.delete_item(TableName=SPECS, Key={PK: {"S": version.spec_id}, SK: {"S": "load"}})


def _alter_version_item(client):
    version = build_spec_version(_dag())
    client.put_item(TableName=SPECS, Item={PK: {"S": version.spec_id}, SK: {"S": "load"},
                                           VERSION_HASH: {"S": version.hashes["extract"]}})


def _drop_text(client):
    client.delete_item(TableName=SPECS, Key={PK: {"S": "orders#text"},
                                             SK: {"S": build_spec_version(_dag()).hashes["load"]}})


def _alter_text(client):
    version = build_spec_version(_dag())
    client.put_item(TableName=SPECS, Item={PK: {"S": "orders#text"}, SK: {"S": version.hashes["load"]},
                                           TEXT: {"S": version.texts["load"].replace("glue", "batch")}})


@pytest.mark.parametrize("fault", [_drop_version_item, _alter_version_item, _drop_text, _alter_text])
def test_specs_dropped_or_altered_stop_the_deploy_before_the_change_set(mocker, platform_dynamodb, fault, capsys):
    cfn = _platform(mocker, _Faulty(platform_dynamodb, fault))
    with pytest.raises(SystemExit) as stopped:
        deploy_pipeline(_dag(), stage="dev", region="us-east-1")
    assert stopped.value.code == 1
    cfn.create_change_set.assert_not_called()
    assert "could not be confirmed" in capsys.readouterr().out


def test_an_access_denied_on_the_specs_stops_the_deploy_and_names_the_permissions(mocker, platform_dynamodb, capsys):
    ddb = mocker.MagicMock(wraps=platform_dynamodb)
    ddb.batch_get_item.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "BatchGetItem")
    cfn = _platform(mocker, ddb)
    with pytest.raises(SystemExit):
        deploy_pipeline(_dag(), stage="dev", region="us-east-1")
    cfn.create_change_set.assert_not_called()
    out = capsys.readouterr().out
    assert all(action in out for action in deploy.SPECS_TABLE_ACTIONS)


def test_a_dry_run_writes_no_spec(mocker, platform_dynamodb, capsys):
    _platform(mocker, platform_dynamodb)
    deploy_pipeline(_dag(), stage="dev", region="us-east-1", dry_run=True)
    assert platform_dynamodb.scan(TableName=SPECS)["Items"] == []
    assert build_spec_version(_dag()).spec_id in capsys.readouterr().out


def test_a_platform_without_the_specs_table_is_refused(mocker, platform_dynamodb, capsys):
    cfn = _platform(mocker, platform_dynamodb, outputs={
        "DependencyWrapperArn": ARN, "OrchestrationRoleArn": "arn:aws:iam::000000000000:role/exec"})
    with pytest.raises(SystemExit):
        deploy_pipeline(_dag(), stage="dev", region="us-east-1")
    cfn.create_change_set.assert_not_called()
    assert "PipelineSpecsTable" in capsys.readouterr().out


# ── the deploy role's documented permissions are the calls deploy makes ──


def _documented_actions_on_specs():
    """The actions of the deploy policy in IAM_PERMISSIONS.md whose resource is pipeline-specs."""
    section = IAM_DOC.read_text().split("## Deploy — per-pipeline `polyris-deploy`", 1)[1].split("\n## ", 1)[0]
    policy = json.loads(re.search(r"```json\n(.*?)```", section, re.S).group(1))
    actions = set()
    for statement in policy["Statement"]:
        resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
        if any(r.endswith("-polyris-pipeline-specs") for r in resources):
            actions |= set(statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]])
    return actions


def _iam_action(method):
    return "dynamodb:" + "".join(part.capitalize() for part in method.split("_"))


def test_the_deploy_policy_allows_exactly_the_calls_deploy_makes_on_the_specs(mocker, platform_dynamodb):
    calls = []

    class _Recording:
        def __getattr__(self, name):
            calls.append(name)
            return getattr(platform_dynamodb, name)

    # Two deploys: the first writes new texts (PutItem), the second finds them held.
    for _ in range(2):
        deploy._write_specs(mocker.MagicMock(client=lambda name: _Recording()), SPECS, build_spec_version(_dag()))
    made = {_iam_action(name) for name in calls}
    assert made == set(deploy.SPECS_TABLE_ACTIONS) == _documented_actions_on_specs()
