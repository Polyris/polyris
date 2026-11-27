"""The deploy policy, the platform's managed policy that a pipeline author holds (ADR #142 §1, §2).

The author held `iam:CreateRole`, `PutRolePolicy` and `UpdateAssumeRolePolicy` on `*-polyris-*-scheduler-role`
and `states:*` on any state machine, and so could make a role with any rights and run it (ADR #142,
*Context*). The author now holds no IAM write action except the migration grant, which goes in 1.5, and passes
two named roles, each to its one service.
"""
from __future__ import annotations

import importlib

from sam_template import load_template

import polyris
from polyris.platform import release_series

MIGRATION = ("MigrationPassTheOldSchedulerRoles", "MigrationRemoveTheOldSchedulerRoles")
OLD_SCHEDULER_ROLES = {"!Sub": "arn:aws:iam::${AWS::AccountId}:role/*-polyris-*-scheduler-role"}


TEMPLATE = load_template(tagged=True)
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


#: The code that serves the move from the scheduler roles of 1.3, which goes with the grant (ADR #142 §7): the
#: preflight's scan for planted roles and the rules it applies, the notice of the update that crosses into 1.4,
#: with its line that asks for a redeploy before 1.5.
MIGRATION_CODE = (
    ("polyris.up", "_foreign_scheduler_roles"), ("polyris.up", "_owning_stack"),
    ("polyris.up", "_crosses_into_1_4"), ("polyris.up", "_print_deploy_policy_notice"),
    ("polyris.up", "REDEPLOY_BEFORE_1_5"),
    ("polyris.platform", "SchedulerRole"), ("polyris.platform", "scheduler_role_problems"),
    ("polyris.platform", "scheduler_role_region"), ("polyris.platform", "is_scheduler_role"),
    ("polyris.platform", "SCHEDULER_ROLE_PATTERN"), ("polyris.platform", "SCHEDULER_ROLE_LOGICAL_ID"),
    ("polyris.platform", "PIPELINE_MACHINE_LOGICAL_ID"), ("polyris.platform", "SCHEDULER_SERVICE"),
)


def _present(module: str, name: str) -> bool:
    return hasattr(importlib.import_module(module), name)


def test_the_migration_grant_goes_in_1_5():
    """ADR #142 §7: the grant lets the first redeploy of a pipeline from before 1.4 delete its own
    scheduler role, for one release. From 1.5.0 this fails until both statements and the code that serves
    them go."""
    if release_series(polyris.__version__) < (1, 5):
        return
    left = sorted(f"{module}.{name}" for module, name in MIGRATION_CODE if _present(module, name))
    assert not set(MIGRATION) & set(BY_SID) and not left, (
        "ADR #142 §7: 1.5 removes the deploy policy's migration grant, the code that serves it "
        f"({', '.join(left) or 'gone'}) and this guard; polyris-up 1.5 refuses while a pipeline stack still holds "
        "PipelineSchedulerRole")


def test_the_guard_reads_the_statements_and_the_code_that_hold_the_grant_today():
    """So a rename cannot leave the guard checking nothing."""
    if release_series(polyris.__version__) < (1, 5):
        assert set(MIGRATION) <= set(BY_SID)
        assert [f"{m}.{n}" for m, n in MIGRATION_CODE if not _present(m, n)] == []


#: What AWS asks of whoever creates or updates a state machine that logs to CloudWatch Logs: Step Functions
#: is a "V1 permissions" vended-logs source (CloudWatch Logs, *Logging that requires additional
#: permissions [V1]*), and its own guide asks the same of the state machine's role (*IAM Policies for logging
#: to CloudWatch Logs*). Every pipeline state machine logs (deploy.py, LoggingConfiguration).
LOG_DELIVERY = {"logs:CreateLogDelivery", "logs:GetLogDelivery", "logs:UpdateLogDelivery",
                "logs:DeleteLogDelivery", "logs:ListLogDeliveries"}
LOG_GROUP_POLICY = {"logs:PutResourcePolicy", "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"}


def _allowed(statements) -> dict:
    """{action: [its resources]} of the Allow statements."""
    allowed: dict = {}
    for statement in statements:
        if statement["Effect"] == "Allow":
            resources = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
            for action in _actions(statement):
                allowed.setdefault(action, []).extend(resources)
    return allowed


def test_the_author_may_set_up_the_log_delivery_of_a_pipelines_state_machine():
    allowed = _allowed(STATEMENTS)
    for action in LOG_DELIVERY:
        assert allowed.get(action) == ["*"], action        # these actions take no resource type
    log_groups = {"!Sub": "arn:aws:logs:${AWS::Region}:${AWS::AccountId}:log-group:*"}
    for action in LOG_GROUP_POLICY - {"logs:DescribeLogGroups"}:
        assert allowed.get(action) == [log_groups], action  # the scope AWS documents for them
    assert "logs:DescribeLogGroups" in allowed


def test_the_state_machines_role_may_set_it_up_too():
    allowed = _allowed(TEMPLATE["Resources"]["OrchestrationRolePolicy"]["Properties"]["PolicyDocument"]["Statement"])
    assert LOG_DELIVERY | LOG_GROUP_POLICY <= set(allowed)
