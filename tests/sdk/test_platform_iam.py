"""The platform's roles that a pipeline and its schedule run as (PLAN_IAM 1.4.0 §3.1–§3.3, D11).

A pipeline author can run any ASL under the orchestration role, so its grants, not the DSL, are the
boundary (IAM_HOLES_VERIFIED). These pin what each role may never do; the deny lists' names are pinned to
``platform.PLATFORM_NAMES`` in ``test_platform_names.py``, the deploy policy in ``test_deploy_policy.py``,
each work state of run_task in ``test_run_task_work_iam.py``.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from polyris.platform import NAMESPACE_STAGE_MAX_LEN

REPO_ROOT = Path(__file__).resolve().parents[2]


def _template() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return {node.tag: loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {node.tag: loader.construct_sequence(node)}
        return loader.construct_mapping(node)

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Condition", "!Select", "!Split", "!ImportValue", "!FindInMap"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load((REPO_ROOT / "sam" / "template.yaml").read_text(), Loader=_Loader)


TEMPLATE = _template()
RESOURCES = TEMPLATE["Resources"]
THIS_ACCOUNT = {"StringEquals": {"aws:SourceAccount": {"!Ref": "AWS::AccountId"}}}


def _statements(resource: str) -> list:
    properties = RESOURCES[resource]["Properties"]
    if "PolicyDocument" in properties:
        return properties["PolicyDocument"]["Statement"]
    return [s for p in properties["Policies"] for s in p["PolicyDocument"]["Statement"]]


def _actions(statement: dict) -> list:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else actions


def _allowed_with(statements: list, action: str) -> list:
    return [s for s in statements if s["Effect"] == "Allow" and action in _actions(s)]


def _trust(role: str) -> list:
    return RESOURCES[role]["Properties"]["AssumeRolePolicyDocument"]["Statement"]


# ── The schedule role (§3.1) ────────────────────────────────────────────────────────────────────────


def test_the_schedule_role_is_trusted_by_scheduler_of_this_account_alone():
    (trust,) = _trust("PipelineScheduleRole")
    assert trust["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert trust["Condition"] == THIS_ACCOUNT


def test_the_schedule_role_starts_pipelines_and_does_nothing_else():
    allows = [s for s in _statements("PipelineScheduleRole") if s["Effect"] == "Allow"]
    assert [(_actions(s), s["Resource"]) for s in allows] == [(["states:StartExecution"], {
        "!Sub": "arn:aws:states:${AWS::Region}:${AWS::AccountId}:stateMachine:*-polyris-*"})]


def test_the_schedule_roles_name_fits_iam_and_stays_out_of_the_migration_grant():
    """The migration grant reaches `*-polyris-*-scheduler-role`; the shared role must stay out of its reach."""
    name = RESOURCES["PipelineScheduleRole"]["Properties"]["RoleName"]["!Sub"]
    assert name == "${Namespace}-${Stage}-polyris-schedule-role"
    assert not name.endswith("-scheduler-role")
    assert len(name.replace("${Namespace}", "").replace("${Stage}", "")) + NAMESPACE_STAGE_MAX_LEN <= 64


def test_the_schedule_role_is_exported_for_the_pipeline_stacks_to_import():
    """A pipeline stack imports it (D8): CloudFormation keeps the platform from dropping it while one does."""
    output = TEMPLATE["Outputs"]["PipelineScheduleRoleArn"]
    assert output["Value"] == {"!GetAtt": "PipelineScheduleRole.Arn"}
    assert output["Export"]["Name"] == {"!Sub": "${Namespace}-${Stage}-polyris-schedule-role-arn"}


# ── The orchestration role (§3.2) ───────────────────────────────────────────────────────────────────


def test_the_orchestration_role_is_trusted_by_step_functions_of_this_account_alone():
    """It was trusted by API Gateway, EventBridge and Scheduler too, and an author could pass it to them."""
    (trust,) = _trust("OrchestrationRole")
    assert trust["Principal"] == {"Service": "states.amazonaws.com"}
    assert trust["Condition"] == THIS_ACCOUNT


def test_the_orchestration_role_assumes_no_role_of_this_account_but_the_default_task_role_and_polyris_tasks():
    """A same-account role= reached any role that trusts the account, a CDK deploy role among them (claim 2, D11)."""
    (deny,) = [s for s in _statements("OrchestrationRolePolicy") if s.get("Sid") == "DenyAssumeRoleInThisAccount"]
    assert deny["Effect"] == "Deny" and _actions(deny) == ["sts:AssumeRole"]
    assert deny["NotResource"] == [{"!GetAtt": "CrossAccountPipelineRole.Arn"},
                                   {"!Sub": "arn:aws:iam::${AWS::AccountId}:role/polyris-tasks/*"}]
    # IfExists: a request without the key is denied, so a gap fails closed (V-C).
    assert deny["Condition"] == {"StringEqualsIfExists": {"aws:ResourceAccount": {"!Ref": "AWS::AccountId"}}}


def test_the_roles_a_pipeline_runs_as_change_no_eventbridge_rule_but_the_managed_ones():
    """A rule of a pipeline's own, targeting a platform function, would go round the function deny."""
    for policy in ("OrchestrationRolePolicy", "CrossAccountPipelinePolicy"):
        changes = [s for s in _statements(policy) if s["Effect"] == "Allow"
                   and {"events:PutRule", "events:PutTargets"} & set(_actions(s))]
        assert changes, policy
        for statement in changes:
            assert statement["Resource"] == {
                "!Sub": "arn:aws:events:${AWS::Region}:${AWS::AccountId}:rule/StepFunctions*"}, policy


def test_the_roles_a_pipeline_runs_as_pass_a_role_to_ecs_tasks_alone():
    for policy in ("OrchestrationRolePolicy", "CrossAccountPipelinePolicy"):
        passes = _allowed_with(_statements(policy), "iam:PassRole")
        assert [s["Condition"] for s in passes] == [
            {"StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}}], policy
        assert all(_actions(s) == ["iam:PassRole"] for s in passes), policy


# ── The default task role (§3.3) ────────────────────────────────────────────────────────────────────


def test_the_default_task_role_describes_and_stops_executions_by_their_arn():
    (statement,) = _allowed_with(_statements("CrossAccountPipelinePolicy"), "states:StopExecution")
    assert set(_actions(statement)) == {"states:DescribeExecution", "states:StopExecution"}
    assert statement["Resource"] == {"!Sub": "arn:aws:states:${AWS::Region}:${AWS::AccountId}:execution:*"}
