"""Every work state of ``run_task`` has its calls on the role it runs them as.

A work state (``Run_Task_<type>``) calls the task's service under ``Credentials``: the default task role, or
the task's ``role=``. The default task role allowed EMR's ``RunJobFlow`` and ``TerminateJobFlows``, which
nothing calls, and not ``AddJobFlowSteps``, which ``Run_Task_EMR`` calls, so every same-account EMR task
failed (IAM_HOLES_VERIFIED, claim 5). Each work state's integration needs the actions below (the Step
Functions documentation of each service integration, a ``.sync`` one with its poll, its stop and, where it
waits on an event, the managed rule); a work state or an action without its grant fails here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_TASK = REPO_ROOT / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"

_MANAGED_RULES = {"events:PutTargets", "events:PutRule", "events:DescribeRule"}
#: What each integration of a work state calls.
NEEDS = {
    "arn:aws:states:::states:startExecution.sync:2":
        {"states:StartExecution", "states:DescribeExecution", "states:StopExecution"} | _MANAGED_RULES,
    "arn:aws:states:::lambda:invoke": {"lambda:InvokeFunction"},
    "arn:aws:states:::glue:startJobRun.sync":
        {"glue:StartJobRun", "glue:GetJobRun", "glue:GetJobRuns", "glue:BatchStopJobRun"},
    "arn:aws:states:::ecs:runTask.sync":
        {"ecs:RunTask", "ecs:StopTask", "ecs:DescribeTasks", "iam:PassRole"} | _MANAGED_RULES,
    "arn:aws:states:::athena:startQueryExecution.sync":
        {"athena:StartQueryExecution", "athena:GetQueryExecution", "athena:StopQueryExecution"},
    "arn:aws:states:::elasticmapreduce:addStep.sync":
        {"elasticmapreduce:AddJobFlowSteps", "elasticmapreduce:DescribeStep", "elasticmapreduce:CancelSteps"}
        | _MANAGED_RULES,
    "arn:aws:states:::batch:submitJob.sync":
        {"batch:SubmitJob", "batch:DescribeJobs", "batch:TerminateJob"} | _MANAGED_RULES,
}


def _work_states() -> dict:
    """{state name: its Resource} of every state of run_task that runs under Credentials."""
    states = json.loads(re.sub(r"\$\{[^}]+\}", "0", RUN_TASK.read_text()))["States"]
    return {name: state["Resource"] for name, state in states.items() if "Credentials" in state}


def _default_task_role_allows() -> set:
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
    template = yaml.load((REPO_ROOT / "sam" / "template.yaml").read_text(), Loader=_Loader)
    allowed: set = set()
    for statement in template["Resources"]["CrossAccountPipelinePolicy"]["Properties"]["PolicyDocument"]["Statement"]:
        if statement["Effect"] == "Allow":
            actions = statement["Action"]
            allowed.update([actions] if isinstance(actions, str) else actions)
    return allowed


@pytest.mark.parametrize("state", sorted(_work_states()))
def test_the_default_task_role_allows_what_the_work_state_calls(state):
    resource = _work_states()[state]
    assert resource in NEEDS, f"{state} calls {resource}: add what it needs to NEEDS"
    missing = NEEDS[resource] - _default_task_role_allows()
    assert not missing, f"{state} calls {sorted(missing)}, which the default task role does not allow"


def test_every_task_type_has_its_work_state():
    """The scan finds the work states: a rename cannot leave it checking nothing."""
    assert set(_work_states()) == {"Run_Task_SFN", "Run_Task_Lambda", "Run_Task_Glue", "Run_Task_ECS",
                                   "Run_Task_Athena", "Run_Task_EMR", "Run_Task_Batch"}


def test_the_default_task_role_creates_and_ends_no_cluster():
    """No work state creates or ends an EMR cluster; RunJobFlow passes any service role and instance profile."""
    assert not {"elasticmapreduce:RunJobFlow", "elasticmapreduce:TerminateJobFlows"} & _default_task_role_allows()
