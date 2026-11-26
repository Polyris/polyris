"""The orchestration role allows every call a direct step makes.

A pipeline's direct steps (``polyris.steps``) run in its own state machine, under the platform's
orchestration role, on the resources the pipeline names. The role allowed Lambda, Step Functions,
ECS, Batch, Glue, Athena and EMR on any resource, but no SNS, SQS or Bedrock call, and S3 and
DynamoDB only on the platform's own bucket and tables: SNSTask, SQSTask, BedrockTask and the S3
and DynamoDB steps on a user's resources failed with AccessDenied (the docs audit after 1.3).
Each step is generated as polyris writes it, and every action its Resource needs must be allowed
on any resource, so a new step without its grant fails here.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from polyris.generators import _generate_step_state
from polyris.steps import (
    AthenaTask, BedrockTask, DynamoDBTask, ECSTask, EventBridgeTask, GlueTask, HttpTask,
    LambdaTask, S3Task, SNSTask, SQSTask,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

STEPS = [
    SNSTask(topic_arn="arn:aws:sns:us-east-1:123456789012:orders", message="done"),
    SQSTask(queue_url="https://sqs.us-east-1.amazonaws.com/123456789012/orders", message_body="done"),
    BedrockTask(model_id="anthropic.claude-3-haiku-20240307-v1:0", body={"prompt": "hi"}),
    EventBridgeTask(source="my.app", detail_type="OrderPlaced"),
    HttpTask(url="https://api.example.com/hook", connection_arn="arn:aws:events:us-east-1:123456789012:connection/hook/0a1b"),
    LambdaTask(function_arn="arn:aws:lambda:us-east-1:123456789012:function:notify"),
    GlueTask(job_name="nightly"),
    AthenaTask(query_string="SELECT 1", database="db", output_location="s3://results/athena/"),
    ECSTask(cluster="etl", task_definition="transform", subnets=["subnet-1"]),
    *[S3Task(operation=op, bucket="data", key="orders.csv", body="x", copy_source="raw/orders.csv")
      for op in ("get_object", "put_object", "copy_object", "delete_object")],
    DynamoDBTask(operation="get_item", table_name="orders", key={"id": {"S": "1"}}),
    DynamoDBTask(operation="put_item", table_name="orders", item={"id": {"S": "1"}}),
    DynamoDBTask(operation="update_item", table_name="orders", key={"id": {"S": "1"}}, update_expression="SET a = :a"),
    DynamoDBTask(operation="delete_item", table_name="orders", key={"id": {"S": "1"}}),
    DynamoDBTask(operation="query", table_name="orders", key_condition="id = :v"),
    DynamoDBTask(operation="scan", table_name="orders"),
]

#: Integrations whose IAM actions are not the operation's own name.
_SPECIAL = {
    "lambda:invoke": {"lambda:InvokeFunction"},
    "s3:copyObject": {"s3:GetObject", "s3:PutObject"},
    "http:invoke": {"states:InvokeHTTPEndpoint", "events:RetrieveConnectionCredentials",
                    "secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"},
}


def _actions(resource: str) -> set:
    """The IAM actions a Step Functions integration ARN needs, e.g. arn:aws:states:::sns:publish."""
    integration = resource.split(":::", 1)[1].split(".")[0]
    integration = integration.removeprefix("aws-sdk:")
    if integration in _SPECIAL:
        return _SPECIAL[integration]
    service, operation = integration.split(":")
    return {f"{service}:{operation[0].upper()}{operation[1:]}"}


def _template() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Condition", "!Select", "!Split", "!ImportValue", "!FindInMap"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load((REPO_ROOT / "sam" / "template.yaml").read_text(), Loader=_Loader)


#: The one resource a step may need its actions on that is narrower than any: HttpTask reads the credentials of
#: the EventBridge connection it names, which AWS keeps in a secret of the connection's own (PLAN_IAM 1.4.0 §3.2).
CONNECTION_SECRETS = "arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:events!connection/*"


def _allowed_on_any_resource() -> set:
    policy = _template()["Resources"]["OrchestrationRolePolicy"]["Properties"]["PolicyDocument"]
    allowed: set = set()
    for statement in policy["Statement"]:
        actions = statement["Action"]
        actions = [actions] if isinstance(actions, str) else actions
        if statement.get("Effect") != "Allow" or "Condition" in statement:
            continue
        if statement.get("Resource") == "*":
            allowed.update(actions)
        elif statement.get("Resource") == CONNECTION_SECRETS:
            allowed.update(a for a in actions if a.startswith("secretsmanager:"))
    return allowed


def test_the_role_reads_no_secret_but_a_connections():
    """Secrets on any resource let every pipeline author read every secret of the account with one state
    (IAM_HOLES_VERIFIED, claim 6a)."""
    policy = _template()["Resources"]["OrchestrationRolePolicy"]["Properties"]["PolicyDocument"]
    resources = [st["Resource"] for st in policy["Statement"]
                 if any(a.startswith("secretsmanager:") for a in ([st["Action"]] if isinstance(st["Action"], str)
                                                                  else st["Action"]))]
    assert resources == [CONNECTION_SECRETS]


@pytest.mark.parametrize("step", STEPS, ids=lambda s: f"{type(s).__name__}:{getattr(s, 'operation', '')}")
def test_the_orchestration_role_allows_what_the_step_calls(step):
    needed = _actions(_generate_step_state(step)["Resource"])
    missing = needed - _allowed_on_any_resource()
    assert not missing, f"{type(step).__name__} calls {sorted(missing)}, which the orchestration role does not allow"


def test_every_direct_step_class_is_checked():
    """A step class added to polyris.steps joins STEPS, or this fails."""
    import polyris.steps as steps_module
    from polyris.steps import Step

    services = {cls.__name__ for cls in vars(steps_module).values()
                if isinstance(cls, type) and issubclass(cls, Step) and cls.__name__.endswith("Task")}
    assert services == {type(step).__name__ for step in STEPS}


@pytest.mark.parametrize("policy", ["OrchestrationRolePolicy", "CrossAccountPipelinePolicy"])
def test_the_direct_steps_never_reach_the_platforms_own_data(policy):
    """AllowDirectSteps reaches any table and bucket, and the default task role any bucket. The console's
    tokens, the task specs, the dependency pointers, the console's files, the release's artifacts and the
    pipeline templates a deployer is about to run stay out of reach, of every platform in the account (D9): a
    DynamoDBTask could write the console an admin token, an S3Task the console's files or the artifacts the
    next platform update deploys (the break review of ADR #140). Neither role uses them."""
    statements = {s.get("Sid"): s for s in _template()["Resources"][policy]["Properties"]["PolicyDocument"]["Statement"]}
    tables, files = statements["DenyDirectStepsOnPlatformData"], statements["DenyDirectStepsOnPlatformFiles"]
    assert tables["Effect"] == files["Effect"] == "Deny"
    item_actions = {"dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
                    "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem"}
    assert set(tables["Action"]) == item_actions
    assert {"s3:PutObject", "s3:DeleteObject"} <= set(files["Action"])
    assert sorted(tables["Resource"]) == sorted(f"arn:aws:dynamodb:*:*:table/*-polyris-{t}*"
                                                for t in ("api-tokens", "pipeline-specs", "dep-subscriptions"))
    assert {"arn:aws:s3:::*-polyris-console-ui/*", "arn:aws:s3:::polyris-artifacts-*/*",
            "arn:aws:s3:::*-polyris-results/cfn-pipeline-templates/*"} <= {r for r in files["Resource"] if isinstance(r, str)}
    assert any("ArtifactsBucket" in str(r) for r in files["Resource"])
