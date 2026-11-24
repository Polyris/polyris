"""polyris-destroy stops a pipeline's running runs until its stack is gone (ADR #137 §4).

A run keeps its execution open until every task of it is final, a paused run forever, and
CloudFormation deletes a state machine only once its executions have ended: before this, a destroy
waited ten minutes and failed, with the stack left deleting (the review of the branch, B-1).
CloudFormation is a small fake (the boto3 boundary); Step Functions is ``fake_states.FakeStates``
behind a real boto3 client.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import pytest
from botocore.exceptions import ClientError

from fake_states import FakeStates
from polyris import cfn_changeset, deploy
from polyris.executions import STATE_MACHINE

STACK = "acme-dev-polyris-sales-etl"
PAUSED_SINCE = datetime(2026, 11, 20, 6, tzinfo=timezone.utc)
SCHEDULED = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)
IAM_DOC = Path(__file__).resolve().parents[2] / "docs" / "deployment" / "IAM_PERMISSIONS.md"


class _PipelineStack:
    """The pipeline's stack: its state machine, then the statuses it shows while CloudFormation deletes it.

    ``on_poll`` is called with the number of each poll after the delete: what happens meanwhile.
    """

    def __init__(self, machine: Optional[str], statuses: List[str], on_poll: Optional[Callable[[int], None]] = None):
        self.machine = machine
        self.statuses = list(statuses)
        self.on_poll = on_poll
        self.polls = 0
        self.calls: List[str] = []
        #: Every CloudFormation operation destroy called, by its IAM name.
        self.made: Set[str] = set()

    def get_paginator(self, name: str):
        assert name == "list_stack_resources"
        self.made.add("cloudformation:ListStackResources")
        fake = self

        class _Pages:
            def paginate(self, StackName: str):
                if fake.machine is None:
                    raise ClientError({"Error": {"Code": "ValidationError",
                                                 "Message": f"Stack with id {StackName} does not exist"}},
                                      "ListStackResources")
                return [{"StackResourceSummaries": [{"LogicalResourceId": "PipelineStateMachine",
                                                     "ResourceType": STATE_MACHINE,
                                                     "PhysicalResourceId": fake.machine}]}]
        return _Pages()

    def describe_stack_events(self, StackName: str) -> Dict[str, Any]:
        self.made.add("cloudformation:DescribeStackEvents")
        return {"StackEvents": []}

    def delete_stack(self, StackName: str) -> None:
        self.made.add("cloudformation:DeleteStack")
        self.calls.append("delete_stack")

    def describe_stacks(self, StackName: str) -> Dict[str, Any]:
        self.made.add("cloudformation:DescribeStacks")
        if "delete_stack" in self.calls:
            self.polls += 1
            if self.on_poll:
                self.on_poll(self.polls)
            if not self.statuses:
                raise ClientError({"Error": {"Code": "ValidationError",
                                             "Message": f"Stack with id {StackName} does not exist"}},
                                  "DescribeStacks")
            return {"Stacks": [{"StackName": StackName, "StackStatus": self.statuses.pop(0)}]}
        return {"Stacks": [{"StackName": StackName, "StackStatus": "UPDATE_COMPLETE"}]}


class _Session:
    def __init__(self, cfn: _PipelineStack, states: FakeStates):
        self.clients = {"cloudformation": cfn, "stepfunctions": states.client}

    def client(self, name: str):
        return self.clients[name]


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(cfn_changeset, "STACK_WAIT_DELAY_S", 0)


def test_destroy_stops_a_paused_run_and_one_its_schedule_starts_while_the_stack_is_deleted(capsys):
    states = FakeStates()
    machine = states.machine(STACK)
    states.run(STACK, "sales-etl-run-2026-11-20-a1b2c3d4", started=PAUSED_SINCE)

    def schedule_fires(poll: int) -> None:
        if poll == 1:     # before CloudFormation reaches the state machine, its schedule starts a run
            states.run(STACK, "sales-etl-run-2026-11-24-e5f6a7b8", started=SCHEDULED)

    cfn = _PipelineStack(machine, ["DELETE_IN_PROGRESS", "DELETE_IN_PROGRESS"], on_poll=schedule_fires)

    deploy.destroy_stack(_Session(cfn, states), STACK)

    assert states.stopped == ["sales-etl-run-2026-11-20-a1b2c3d4", "sales-etl-run-2026-11-24-e5f6a7b8"]
    assert {(s["error"], s["cause"]) for s in states.stops} == {(deploy.DESTROY_STOP_ERROR, deploy.DESTROY_STOP_CAUSE)}
    out = capsys.readouterr().out
    assert "Stopped run sales-etl-run-2026-11-20-a1b2c3d4 (started 2026-11-20 06:00 UTC)" in out
    assert "Stopped run sales-etl-run-2026-11-24-e5f6a7b8" in out
    assert "The engine stops the tasks of each stopped run" in out
    assert f"Stack deleted: {STACK}" in out


def test_destroy_that_may_not_list_the_runs_exits_before_it_deletes_anything(capsys):
    states = FakeStates()
    machine = states.machine(STACK)
    states.denied = {"ListExecutions"}
    cfn = _PipelineStack(machine, [])

    with pytest.raises(SystemExit) as exited:
        deploy.destroy_stack(_Session(cfn, states), STACK)

    assert exited.value.code == 1
    assert "delete_stack" not in cfn.calls
    assert "ListExecutions" in capsys.readouterr().out


def test_destroy_of_a_stack_that_is_gone_already_stops_nothing_and_succeeds(capsys):
    states = FakeStates()
    cfn = _PipelineStack(None, [])

    deploy.destroy_stack(_Session(cfn, states), STACK)

    assert states.stopped == []
    out = capsys.readouterr().out
    assert f"Stack deleted: {STACK}" in out
    assert "The engine stops" not in out


def _deploy_policy_actions() -> Set[str]:
    """Every action of the deploy policy in IAM_PERMISSIONS.md."""
    section = IAM_DOC.read_text().split("## Deploy — per-pipeline `polyris-deploy`", 1)[1].split("\n## ", 1)[0]
    policy = json.loads(re.search(r"```json\n(.*?)```", section, re.S).group(1))
    actions: Set[str] = set()
    for statement in policy["Statement"]:
        listed = statement["Action"]
        actions |= set(listed if isinstance(listed, list) else [listed])
    return actions


class _Tables:
    """DynamoDB's delete_item, which refuses with ``refuse`` when it is set."""

    def __init__(self, refuse: Optional[str] = None):
        self.deleted: List[tuple] = []
        self.refuse = refuse

    def delete_item(self, TableName: str, Key: Dict[str, Any]) -> Dict[str, Any]:
        if self.refuse:
            raise ClientError({"Error": {"Code": self.refuse, "Message": "no"}}, "DeleteItem")
        self.deleted.append((TableName, {k: v["S"] for k, v in Key.items()}))
        return {}


def test_destroy_removes_the_pipeline_from_the_console(capsys):
    """The console lists a pipeline by its registry row: a destroyed pipeline stayed listed until someone deleted
    the row by hand, and its asset subscriptions until the next fact of each asset."""
    tables = _Tables()
    session = _Session(_PipelineStack(None, []), FakeStates())
    session.clients["dynamodb"] = tables

    assert deploy.unregister_pipeline(session, "sales-etl", registry_table="ns-dev-polyris-pipeline-registry",
                                      subscriptions_table="ns-dev-polyris-asset-subscriptions",
                                      assets=["ns/orders", "ns/customers"])

    assert tables.deleted == [
        ("ns-dev-polyris-pipeline-registry", {"pipeline_name": "sales-etl"}),
        ("ns-dev-polyris-asset-subscriptions", {"asset_name": "ns/orders", "pipeline_name": "sales-etl"}),
        ("ns-dev-polyris-asset-subscriptions", {"asset_name": "ns/customers", "pipeline_name": "sales-etl"}),
    ]
    assert "Removed sales-etl from the console" in capsys.readouterr().out


def test_a_refused_removal_says_so_and_how_after_the_stack_is_gone(capsys):
    session = _Session(_PipelineStack(None, []), FakeStates())
    session.clients["dynamodb"] = _Tables(refuse="AccessDeniedException")

    assert not deploy.unregister_pipeline(session, "sales-etl", registry_table="r", subscriptions_table="s", assets=[])

    out = capsys.readouterr().out
    assert "The stack is gone, but sales-etl could not be removed from the console" in out
    assert "dynamodb:DeleteItem" in out and "AccessDeniedException" in out


def test_the_deploy_policy_allows_every_call_destroy_makes():
    states = FakeStates()
    machine = states.machine(STACK)
    states.run(STACK, "sales-etl-run-2026-11-20-a1b2c3d4", started=PAUSED_SINCE)
    cfn = _PipelineStack(machine, ["DELETE_IN_PROGRESS"])
    session = _Session(cfn, states)
    session.clients["dynamodb"] = _Tables()

    deploy.destroy_stack(session, STACK)
    deploy.unregister_pipeline(session, "sales-etl", registry_table="r", subscriptions_table="s", assets=["ns/a"])

    made = cfn.made | {f"states:{operation}" for operation in states.operations()} | {"dynamodb:DeleteItem"}
    assert {"states:ListExecutions", "states:StopExecution", "cloudformation:DeleteStack"} <= made
    assert made <= _deploy_policy_actions()
