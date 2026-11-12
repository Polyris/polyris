"""Contract tests for the registration helper (ADR #130 §6.3).

Registration saves the task row, signals a root task at once, and otherwise
writes the run's subscriptions and asks the reconciler. Its expressions are
evaluated here with jsonata-python and compared with the Python code they
must agree with. tests/backend/coordination runs the whole template.
"""

import json
from datetime import timedelta

import pytest
from sfn_jsonata import NOW, evaluate, load_template

from polyris.coordination import attention, wake
from polyris.coordination.times import format_utc

TRANSIENT = {"Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException",
             "Lambda.TooManyRequestsException", wake.TRANSIENT_ERROR}


@pytest.fixture(scope="module")
def template() -> dict:
    return load_template("helpers/registration/sfn.tpl.json")


def _evaluate(expression: str, states_input: dict):
    return evaluate(expression, input=states_input)


def test_no_orphan_states(template):
    """Every state is reachable from StartAt."""
    states, reachable = template["States"], set()

    def walk(name):
        if name in reachable or name not in states:
            return
        reachable.add(name)
        state = states[name]
        for target in [state.get("Next"), state.get("Default"), *[c.get("Next") for c in state.get("Choices", [])],
                       *[c.get("Next") for c in state.get("Catch", [])]]:
            if isinstance(target, str):
                walk(target)

    walk(template["StartAt"])
    assert set(states) == reachable


def test_the_row_carries_what_the_reconciler_sweeper_and_restart_read(template):
    item = template["States"]["Save_Task_Record"]["Arguments"]["Item"]
    for field in ("status", "dependencies", "wait_for", "trigger_rule", "date", "started_at", "wait_token",
                  "wrapper_execution_arn", "pipeline_execution", "pipeline_execution_short",
                  "starter_input", "orchestration_timeout", attention.KEY, attention.DUE):
        assert field in item, field
    assert item["status"] == {"S": "waiting"}


@pytest.mark.parametrize("name", ["load-2026-11-12--2026-11-12-abcdef12", "a", "x" * 200])
def test_the_attention_shard_matches_python(template, name):
    expression = template["States"]["Save_Task_Record"]["Arguments"]["Item"][attention.KEY]["S"]
    assert _evaluate(expression, {"execution_name": name}) == attention.attention(attention.WAITING, name)


def test_the_first_due_time_matches_python(template):
    expression = template["States"]["Save_Task_Record"]["Arguments"]["Item"][attention.DUE]["S"]
    assert _evaluate(expression, {}) == format_utc(NOW + attention.FIRST_DUE)


def test_subscriptions_are_per_run_pointers_without_tokens(template):
    items = _evaluate(template["States"]["Save_Subscriptions"]["Items"], {
        "execution_name": "load-2026-11-12--S", "pipeline_execution_short": "-S", "ttl": 9,
        "dependencies": ["extract"],
        "wait_for": [{"asset_name": "ns/a", "freshness_hours": None},
                     {"operator": "OR", "assets": [{"asset_name": "ns/b"}, {"asset_name": "ns/a"}]}],
    })
    assert items == [
        {"dependency_key": "extract--S", "subscriber": "load-2026-11-12--S", "ttl": 9},
        {"dependency_key": "asset:ns/a", "subscriber": "load-2026-11-12--S", "ttl": 9},
        {"dependency_key": "asset:ns/b", "subscriber": "load-2026-11-12--S", "ttl": 9},
    ]
    written = template["States"]["Save_Subscriptions"]["ItemProcessor"]["States"]["Save_Subscription"]
    assert set(written["Arguments"]["Item"]) == {"dependency_key", "subscriber", "ttl"}


def test_a_root_task_is_signalled_at_once_with_the_ready_signal(template):
    signal = template["States"]["Signal_Root_Task"]["Arguments"]["Output"]
    assert json.loads(signal)["signal"] == wake.DEPS_READY


def test_everything_else_asks_the_reconciler(template):
    reconcile = template["States"]["Reconcile"]
    assert reconcile["Arguments"]["Payload"] == {"execution_name": "{% $states.input.execution_name %}",
                                                 "source": "registration"}
    assert set(reconcile["Retry"][0]["ErrorEquals"]) == TRANSIENT


def test_transient_failures_defer_to_the_sweeper_and_others_fail_the_task(template):
    states = template["States"]
    for name in ("Save_Subscriptions", "Reconcile"):
        first, last = states[name]["Catch"][0], states[name]["Catch"][-1]
        assert first["Next"] == "Defer_To_Sweeper", name
        assert (last["ErrorEquals"], last["Next"]) == (["States.ALL"], "Fail_Task"), name
    assert set(states["Reconcile"]["Catch"][0]["ErrorEquals"]) == TRANSIENT


def test_a_lost_row_always_fails_the_task(template):
    """B-G1: with no row there is nothing for the sweeper to find."""
    [catch] = template["States"]["Save_Task_Record"]["Catch"]
    assert (catch["ErrorEquals"], catch["Next"]) == (["States.ALL"], "Fail_Task")


def test_the_task_fails_with_the_coordination_error(template):
    fail = template["States"]["Fail_Task"]
    assert fail["Resource"].endswith(":sfn:sendTaskFailure")
    assert fail["Arguments"]["Error"] == wake.COORDINATION_ERROR


def test_every_task_state_catches_everything(template):
    for name, state in template["States"].items():
        if state["Type"] in ("Task", "Map"):
            assert any("States.ALL" in c["ErrorEquals"] for c in state.get("Catch", [])), name


def test_due_now_is_pinned_to_the_token_and_a_waiting_status(template):
    defer = template["States"]["Defer_To_Sweeper"]["Arguments"]
    assert defer["ConditionExpression"] == "wait_token = :token AND #s IN (:waiting, :paused)"
    assert defer["UpdateExpression"] == f"SET {attention.DUE} = :now"


def test_due_time_constants_are_what_the_template_uses():
    assert attention.FIRST_DUE == timedelta(minutes=10)
