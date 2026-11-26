"""The deploy policy, the platform's managed policy that a pipeline author holds (PLAN_IAM 1.4.0 §5, D3, D7).

The author held `iam:CreateRole`, `PutRolePolicy` and `UpdateAssumeRolePolicy` on `*-polyris-*-scheduler-role`
and `states:*` on any state machine, and so could make a role with any rights and run it (IAM_HOLES_VERIFIED,
claim 1). The author now holds no IAM write action except the migration grant, which goes in 1.5, and passes
two named roles, each to its one service.
"""
from __future__ import annotations

from pathlib import Path

import yaml

import polyris
from polyris.platform import release_series

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ("MigrationPassTheOldSchedulerRoles", "MigrationRemoveTheOldSchedulerRoles")
OLD_SCHEDULER_ROLES = {"!Sub": "arn:aws:iam::${AWS::AccountId}:role/*-polyris-*-scheduler-role"}


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
POLICY = TEMPLATE["Resources"]["PipelineDeployPolicy"]
STATEMENTS = POLICY["Properties"]["PolicyDocument"]["Statement"]
BY_SID = {s["Sid"]: s for s in STATEMENTS}


def _actions(statement: dict) -> list:
    actions = statement["Action"]
    return [actions] if isinstance(actions, str) else actions


def test_it_is_a_managed_policy_of_the_platform_with_its_arn_in_the_outputs():
    assert POLICY["Type"] == "AWS::IAM::ManagedPolicy"
    assert POLICY["Properties"]["ManagedPolicyName"] == {"!Sub": "${Namespace}-${Stage}-polyris-deploy-policy"}
    assert TEMPLATE["Outputs"]["PipelineDeployPolicyArn"]["Value"] == {"!Ref": "PipelineDeployPolicy"}


def test_every_sid_is_unique():
    assert len(BY_SID) == len(STATEMENTS)


def test_the_author_holds_no_iam_action_but_passing_named_roles_and_the_migration_grant():
    iam = [s for s in STATEMENTS if s["Effect"] == "Allow" and any(a.startswith("iam:") for a in _actions(s))]
    assert {s["Sid"] for s in iam} == {"PassTheOrchestrationRoleToStepFunctionsOnly",
                                       "PassTheScheduleRoleToSchedulerOnly", *MIGRATION}
    removal = BY_SID["MigrationRemoveTheOldSchedulerRoles"]
    assert set(_actions(removal)) == {"iam:GetRole", "iam:DeleteRole", "iam:DeleteRolePolicy",
                                      "iam:ListRolePolicies", "iam:ListAttachedRolePolicies"}
    assert removal["Resource"] == OLD_SCHEDULER_ROLES


def test_each_role_the_author_may_pass_goes_to_one_service():
    passes = {s["Sid"]: (s["Resource"], s["Condition"]) for s in STATEMENTS
              if s["Effect"] == "Allow" and "iam:PassRole" in _actions(s)}
    assert all(_actions(BY_SID[sid]) == ["iam:PassRole"] for sid in passes)
    assert passes == {
        "PassTheOrchestrationRoleToStepFunctionsOnly": (
            {"!GetAtt": "OrchestrationRole.Arn"}, {"StringEquals": {"iam:PassedToService": "states.amazonaws.com"}}),
        "PassTheScheduleRoleToSchedulerOnly": (
            {"!GetAtt": "PipelineScheduleRole.Arn"},
            {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}),
        "MigrationPassTheOldSchedulerRoles": (
            OLD_SCHEDULER_ROLES, {"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}}),
    }


def test_the_author_manages_the_pipelines_state_machines_alone():
    """`states:*` on any resource let an author rewrite run_task, or another team's state machine."""
    for statement in STATEMENTS:
        states = [a for a in _actions(statement) if a.startswith("states:")]
        if statement["Effect"] == "Allow" and states and statement["Resource"] == "*":
            assert states == ["states:DescribeStateMachine"], statement["Sid"]
    assert BY_SID["PipelineStateMachines"]["Resource"] == "arn:aws:states:*:*:stateMachine:*-polyris-*"


def test_the_author_writes_this_platforms_own_tables_and_template_prefix_alone():
    assert BY_SID["PipelineRegistryStamp"]["Resource"] == {"!GetAtt": "PipelineRegistryTable.Arn"}
    assert BY_SID["PipelineAssetSubscriptions"]["Resource"] == {"!GetAtt": "AssetSubscriptionsTable.Arn"}
    assert BY_SID["PipelineTaskSpecs"]["Resource"] == {"!GetAtt": "PipelineSpecsTable.Arn"}
    assert BY_SID["LargeTemplates"]["Resource"] == {"!Sub": "${ResultsBucket.Arn}/cfn-pipeline-templates/*"}


def test_the_author_cannot_change_the_platforms_own_stack():
    deny = BY_SID["NeverThePlatformStack"]
    assert deny["Effect"] == "Deny" and deny["Resource"] == {"!Ref": "AWS::StackId"}
    writes = {a for a in _actions(BY_SID["PipelineStackManagement"])
              if not a.split(":")[1].startswith(("Describe", "Get", "List"))}
    assert writes == set(_actions(deny))


def test_the_migration_grant_goes_in_1_5():
    """PLAN_IAM 1.4.0 §5.3: the grant lets the first redeploy of a pipeline from before 1.4 delete its own
    scheduler role, for one release. From 1.5.0 this fails until both statements go."""
    if release_series(polyris.__version__) < (1, 5):
        return
    assert not set(MIGRATION) & set(BY_SID), (
        "PLAN_IAM 1.4.0 §5.3: 1.5 removes the deploy policy's migration grant (and this guard); "
        "polyris-up 1.5 refuses while a pipeline stack still holds PipelineSchedulerRole")


def test_the_guard_reads_the_statements_that_hold_the_grant_today():
    """So a rename cannot leave the guard checking nothing."""
    if release_series(polyris.__version__) < (1, 5):
        assert set(MIGRATION) <= set(BY_SID)
