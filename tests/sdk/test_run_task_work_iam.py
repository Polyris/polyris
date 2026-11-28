"""Every work state of ``run_task`` has its calls on the role it runs them as.

A work state (``Run_Task_<type>``) calls the task's service under ``Credentials``: the default task role, or
the task's ``role=``. The default task role allowed EMR's ``RunJobFlow`` and ``TerminateJobFlows``, which
nothing calls, and not ``AddJobFlowSteps``, which ``Run_Task_EMR`` calls, so every same-account EMR task
failed (ADR #142 §9). Each work state's integration needs the actions below (the Step
Functions documentation of each service integration, a ``.sync`` one with its poll, its stop and, where it
waits on an event, the managed rule); a work state or an action without its grant fails here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sam_template import load_template

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
    template = load_template(tagged=False)
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


#: The decorator of each integration, as the how-to's table names it.
DECORATORS = {
    "arn:aws:states:::states:startExecution.sync:2": "@task.sfn",
    "arn:aws:states:::lambda:invoke": "@task.lambda_function",
    "arn:aws:states:::glue:startJobRun.sync": "@task.glue_job",
    "arn:aws:states:::ecs:runTask.sync": "@task.ecs_task",
    "arn:aws:states:::athena:startQueryExecution.sync": "@task.athena_query",
    "arn:aws:states:::elasticmapreduce:addStep.sync": "@task.emr_step",
    "arn:aws:states:::batch:submitJob.sync": "@task.batch_job",
}


def test_the_how_to_lists_every_action_a_tasks_own_role_needs_in_the_row_of_its_task():
    """A task's own role (``role=``) makes the same calls as the default task role: the guide for the
    infrastructure engineer who creates one names each of them in the row of the task that makes it, and
    the managed rule for exactly the tasks that need it."""
    guide = (REPO_ROOT / "docs" / "how-to" / "give-a-task-a-role-of-its-own.md").read_text()
    rows = {cells[0].strip("` "): cells[1] for line in guide.splitlines() if line.startswith("| `@task.")
            for cells in [line.strip("|").split("|")]}
    assert set(DECORATORS) == set(NEEDS) and set(rows) == set(DECORATORS.values())
    for resource, needs in NEEDS.items():
        row = rows[DECORATORS[resource]]
        missing = sorted(action for action in needs - _MANAGED_RULES if f"`{action}`" not in row)
        assert missing == [], DECORATORS[resource]
        assert ("managed rule" in row) == bool(needs & _MANAGED_RULES), DECORATORS[resource]
    section = guide.split("### The managed rule", 1)[1].split("\n## ", 1)[0]
    assert all(f"`{action}`" in section for action in _MANAGED_RULES)
