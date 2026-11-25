"""The interpreter's Execution Status Change events and the bus that delivers them (event_bus.py)."""

import json
from typing import Any, Dict, List

import pytest

from event_bus import ASYNC_ATTEMPTS, EventBus
from sfn_runtime import EVENT_PAYLOAD_LIMIT, Runtime

WAITS = {"StartAt": "Wait", "States": {"Wait": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke"
                                                                         ".waitForTaskToken",
                                              "Arguments": {"FunctionName": "f", "Payload": {}}, "End": True}}}
FAILS = {"StartAt": "Fail", "States": {"Fail": {"Type": "Fail", "Error": "ClaimFailed", "Cause": "no claim"}}}
DONE = {"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}


@pytest.fixture
def runtime() -> Runtime:
    rt = Runtime(None, now=lambda: "2026-11-25T06:00:00.000Z")
    rt.lambdas["f"] = lambda event, context: {}
    return rt


def start(rt: Runtime, definition: Dict[str, Any], name: str, payload: Any = None, *, express: bool = False) -> str:
    machine = rt.add_machine(f"acme-dev-polyris-{name}", definition, type="EXPRESS" if express else "STANDARD")
    arn = rt.start_execution(stateMachineArn=machine.arn, name=name, input=json.dumps(payload or {}))["executionArn"]
    rt.run()
    return arn


def test_each_end_of_a_standard_execution_is_an_event_shaped_as_aws_documents_it(runtime):
    stopped = start(runtime, WAITS, "stopped")
    runtime.stop_execution(executionArn=stopped)                      # by hand, without an error
    start(runtime, FAILS, "failed")
    start(runtime, DONE, "done")
    start(runtime, DONE, "express", express=True)                     # Express machines send no event
    assert [(e["detail"]["name"], e["detail"]["status"], e["detail"]["error"]) for e in runtime.events] == [
        ("stopped", "ABORTED", None), ("failed", "FAILED", "ClaimFailed"), ("done", "SUCCEEDED", None)]
    event = runtime.events[0]
    assert (event["source"], event["detail-type"]) == ("aws.states", "Step Functions Execution Status Change")
    assert event["detail"]["input"] == "{}" and event["detail"]["inputDetails"] == {"included": True}
    assert event["detail"]["executionArn"] == stopped and event["resources"] == [stopped]
    # DescribeExecution keeps what it showed before: a stop without an error reads States.Aborted.
    assert runtime.describe_execution(executionArn=stopped)["error"] == "States.Aborted"


def test_an_event_drops_an_input_too_large_for_eventbridge(runtime):
    arn = start(runtime, WAITS, "large", {"blob": "x" * EVENT_PAYLOAD_LIMIT})
    runtime.stop_execution(executionArn=arn, error="Console")
    [event] = runtime.events
    assert event["detail"]["input"] is None and event["detail"]["inputDetails"] == {"included": False}


def bus(runtime: Runtime, calls: List[Dict[str, Any]], *, raises: int = 0) -> EventBus:
    left = [raises]

    def target(event: Dict[str, Any], _: Any) -> Dict[str, Any]:
        calls.append(event)
        if left[0]:
            left[0] -= 1
            raise RuntimeError("transient")
        return {"took": event["detail"]["name"]}

    runtime.lambdas["target"] = target
    return EventBus(runtime, {"source": ["aws.states"], "detail": {"status": ["ABORTED"]}}, "target")


def test_the_bus_delivers_what_the_pattern_matches_once_and_loses_what_a_drop_names(runtime):
    calls: List[Dict[str, Any]] = []
    events = bus(runtime, calls)
    first, second = start(runtime, WAITS, "first"), start(runtime, WAITS, "second")
    start(runtime, DONE, "done")
    events.drop(lambda event: event["detail"]["executionArn"] == second)
    runtime.stop_execution(executionArn=first)
    runtime.stop_execution(executionArn=second)
    assert events.deliver() == 1 and events.deliver() == 0              # each event once
    assert [e["detail"]["name"] for e in calls] == ["first"]
    assert [e["detail"]["name"] for e in events.lost] == ["second"]
    assert [e["detail"]["name"] for e in events.filtered] == ["done"]
    assert events.publish(calls[0]) and events.results[-1] == {"took": "first"}   # one the test sends again


@pytest.mark.parametrize("raises,delivered", [(ASYNC_ATTEMPTS - 1, 1), (ASYNC_ATTEMPTS, 0)])
def test_a_target_that_fails_is_invoked_twice_more_then_the_event_is_dropped(runtime, raises, delivered):
    calls: List[Dict[str, Any]] = []
    events = bus(runtime, calls, raises=raises)
    runtime.stop_execution(executionArn=start(runtime, WAITS, "failing"))
    assert events.deliver() == delivered and len(calls) == ASYNC_ATTEMPTS
    assert len(events.failed) == 1 - delivered


def test_the_bus_of_the_template_targets_the_dispatcher(world):
    """The World's bus is the rule of sam/template.yaml, delivered to the function its target names."""
    rule = world._execution_ends
    assert rule.function == world.stack.functions["ReconcileFunction"]
    assert rule.pattern["detail"]["error"][0] is None
