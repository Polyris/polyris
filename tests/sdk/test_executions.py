"""The stop of the running executions that a stack deletion waits for (``polyris.executions``, ADR #137 §4).

Step Functions is ``fake_states.FakeStates`` behind a real boto3 client, so botocore checks every
request; CloudFormation's resource listing is a small fake (the boto3 boundary).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import pytest
from botocore.exceptions import ClientError

from fake_states import FakeStates
from polyris import executions

EARLY = datetime(2026, 11, 24, 8, tzinfo=timezone.utc)
LATE = datetime(2026, 11, 24, 9, tzinfo=timezone.utc)


def _stop(states: FakeStates, machines, seen: Optional[Set[str]] = None):
    return executions.stop_running(states.client, machines, error="Polyris.Test", cause="a test stopped it",
                                   seen=set() if seen is None else seen)


def test_it_stops_each_running_execution_once_and_names_its_machine():
    states = FakeStates()
    first, second = states.machine("sales-etl"), states.machine("orders-report")
    states.run("sales-etl", "run-1", started=EARLY)
    states.run("orders-report", "run-2", started=EARLY, lingers=1)   # a listing still shows it after its stop
    seen: Set[str] = set()

    stopped = _stop(states, [(first, None), (second, None)], seen)
    again = _stop(states, [(first, None), (second, None)], seen)

    assert [(machine, e["name"]) for machine, e in stopped] == [(first, "run-1"), (second, "run-2")]
    assert again == []
    assert states.stopped == ["run-1", "run-2"]
    assert {(s["error"], s["cause"]) for s in states.stops} == {("Polyris.Test", "a test stopped it")}


def test_it_stops_only_what_started_before_the_time_of_its_machine():
    states = FakeStates()
    machine = states.machine("run-task")
    states.run("run-task", "older", started=EARLY)
    states.run("run-task", "newer", started=LATE)

    stopped = _stop(states, [(machine, LATE)])

    assert [e["name"] for _, e in stopped] == ["older"]
    assert states.running() == ["newer"]


def test_a_deleted_machine_and_an_execution_gone_at_its_stop_stop_nothing_but_count_as_met():
    states = FakeStates()
    machine = states.machine("sales-etl")
    ended = states.run("sales-etl", "ended", started=EARLY)
    states.gone_on_stop.add("ended")
    seen: Set[str] = set()

    assert _stop(states, [(f"{machine}-deleted", None), (machine, None)], seen) == []
    assert seen == {ended.arn}


@pytest.mark.parametrize("operation", ["ListExecutions", "StopExecution"])
def test_a_denied_call_reaches_the_caller(operation):
    states = FakeStates()
    machine = states.machine("sales-etl")
    states.run("sales-etl", "run-1", started=EARLY)
    states.denied = {operation}

    with pytest.raises(ClientError) as raised:
        _stop(states, [(machine, None)])
    assert executions.error_code(raised.value) == "AccessDeniedException"


class _Resources:
    """CloudFormation's ListStackResources, two pages, or the error a call gets."""

    def __init__(self, resources: List[Dict[str, Any]], error: Optional[ClientError] = None):
        self.resources = resources
        self.error = error

    def get_paginator(self, name: str):
        assert name == "list_stack_resources"
        fake = self

        class _Pages:
            def paginate(self, StackName: str):
                if fake.error:
                    raise fake.error
                half = len(fake.resources) // 2
                return [{"StackResourceSummaries": fake.resources[:half]},
                        {"StackResourceSummaries": fake.resources[half:]}]
        return _Pages()


def _resource(logical: str, kind: str, physical: Optional[str]) -> Dict[str, Any]:
    resource: Dict[str, Any] = {"LogicalResourceId": logical, "ResourceType": kind}
    if physical:
        resource["PhysicalResourceId"] = physical
    return resource


def test_the_machines_of_a_stack_are_its_state_machines_that_exist():
    cfn = _Resources([
        _resource("PipelineStateMachine", executions.STATE_MACHINE, "arn:aws:states:eu-central-1:1:stateMachine:a"),
        _resource("PipelineLogGroup", "AWS::Logs::LogGroup", "/aws/states/a"),
        _resource("RunTaskStateMachine", executions.STATE_MACHINE, None),     # never created
        _resource("BackfillStateMachine", executions.STATE_MACHINE, "arn:aws:states:eu-central-1:1:stateMachine:b"),
    ])

    assert executions.stack_machines(cfn, "acme-dev-polyris-sales-etl") == [
        "arn:aws:states:eu-central-1:1:stateMachine:a", "arn:aws:states:eu-central-1:1:stateMachine:b"]


def test_a_stack_that_does_not_exist_has_no_machines_and_another_error_reaches_the_caller():
    def error(code: str, message: str) -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": message}}, "ListStackResources")

    gone = _Resources([], error("ValidationError", "Stack with id acme-dev-polyris-sales-etl does not exist"))
    denied = _Resources([], error("AccessDenied", "not authorized to perform cloudformation:ListStackResources"))

    assert executions.stack_machines(gone, "acme-dev-polyris-sales-etl") == []
    with pytest.raises(ClientError):
        executions.stack_machines(denied, "acme-dev-polyris-sales-etl")
