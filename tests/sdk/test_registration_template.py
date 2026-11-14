"""Contract tests for the registration helper (ADR #130 §6.3).

Registration saves the task row, signals a root task at once, and otherwise
writes the run's subscriptions and asks the reconciler. Its expressions are
evaluated here with jsonata-python and compared with the Python code they
must agree with. tests/backend/coordination runs the whole template.
"""

import json
from datetime import timedelta

import pytest
from sfn_jsonata import LAMBDA_TRANSIENT, NOW, evaluate, load_template

from polyris.coordination import attention, ownership, wake
from polyris.coordination.times import format_utc

#: A Lambda timeout (Sandbox.Timedout) or crash (Lambda.Unknown) leaves the saved row to the sweeper too.
TRANSIENT = LAMBDA_TRANSIENT


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
    kept, lost = template["States"]["Save_Task_Record"]["Catch"]
    assert (kept["ErrorEquals"], kept["Next"]) == (["DynamoDB.ConditionalCheckFailedException"], "Signal_Superseded")
    assert (lost["ErrorEquals"], lost["Next"]) == (["States.ALL"], "Fail_Task")


def test_registration_writes_only_a_new_row_or_one_its_generation_owns(template):
    """ADR #130 §2.1: a raised generation fences every older wrapper; test_who_may_save_the_task_row
    evaluates the condition itself. Here: its values are the ones the row and the reconciler use."""
    from polyris.constants import RECONCILABLE_STATUSES

    arguments = template["States"]["Save_Task_Record"]["Arguments"]
    values = arguments["ExpressionAttributeValues"]
    assert {values[":waiting"]["S"], values[":paused"]["S"]} == set(RECONCILABLE_STATUSES)
    assert _evaluate(values[":self"]["S"], {"wrapper_execution_arn": "arn:w"}) == "arn:w"
    assert arguments["Item"]["wrapper_execution_arn"] == values[":self"]   # the row names the same wrapper
    assert arguments["Item"][ownership.FIELD] == values[":g"]               # and the owner's generation
    assert _evaluate(values[":g"]["N"], {"generation": 3}) == "3"
    assert _evaluate(values[":g"]["N"], {}) == str(ownership.LEGACY)         # a pre-upgrade wrapper


def test_a_kept_row_answers_the_wrapper_superseded(template):
    """A current wrapper routes Superseded to its quiet end; a pre-upgrade one fails into a failure
    handler that finds the row raised (ADR #130 §2.1)."""
    signal = template["States"]["Signal_Superseded"]
    assert signal["Resource"].endswith(":sfn:sendTaskFailure")
    assert _evaluate(signal["Arguments"]["TaskToken"], {"wait_token": "T"}) == "T"
    assert signal["Arguments"]["Error"] == "Superseded"


def test_the_task_fails_with_the_coordination_error(template):
    fail = template["States"]["Fail_Task"]
    assert fail["Resource"].endswith(":sfn:sendTaskFailure")
    assert fail["Arguments"]["Error"] == wake.COORDINATION_ERROR


def test_every_send_to_the_wrapper_is_retried_before_it_gives_up(template):
    """A throttled send is not a lost signal: after a lost row, Fail_Task is all that can reach the wrapper."""
    sends = {name: state for name, state in template["States"].items()
             if state.get("Resource", "").endswith((":sfn:sendTaskSuccess", ":sfn:sendTaskFailure"))}
    assert set(sends) == {"Signal_Root_Task", "Signal_Superseded", "Fail_Task"}
    for name, state in sends.items():
        assert any("States.ALL" in retry["ErrorEquals"] for retry in state.get("Retry", [])), name


def test_every_task_state_catches_everything(template):
    for name, state in template["States"].items():
        if state["Type"] in ("Task", "Map"):
            assert any("States.ALL" in c["ErrorEquals"] for c in state.get("Catch", [])), name


def test_due_now_is_pinned_to_the_token_and_a_waiting_status(template):
    defer = template["States"]["Defer_To_Sweeper"]["Arguments"]
    assert defer["ConditionExpression"] == f"wait_token = :token AND #s IN (:waiting, :paused) AND {ownership.FENCE}"
    assert defer["UpdateExpression"] == f"SET {attention.DUE} = :now"


def test_due_time_constants_are_what_the_template_uses():
    assert attention.FIRST_DUE == timedelta(minutes=10)


#: The input a v1.1.x wrapper passes to the registration helper. Such a wrapper, in
#: flight during the upgrade, registers again when its dependency wait times out,
#: and by then the helper runs the new definition (ADR #130 Migration).
PRE_UPGRADE_WRAPPER_INPUT = {
    "cross_account_role": "", "current_date": "2026-11-12", "date": "2026-11-12", "dependencies": ["extract"],
    "execution_name": "load-2026-11-12--2026-11-12-abcdef12", "orchestration_token": "orch",
    "outlets": [], "pipeline_execution": "orders-run-2026-11-12-abcdef12",
    "pipeline_execution_short": "-2026-11-12-abcdef12", "pipeline_name": "orders",
    "task_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:load", "task_config": {},
    "task_name": "load", "task_type": "sfn", "trigger_rule": "all_success", "triggered_by": "schedule",
    "ttl": 1800000000, "wait_before": 0, "wait_for": [], "wait_token": "T",
    "wrapper_execution_arn": "arn:aws:states:us-east-1:123456789012:execution:wrapper:load",
}


def test_a_pre_upgrade_wrapper_can_register_again(template):
    """Every field of the row evaluates for the old input; an undefined value fails
    the state in AWS (States.QueryEvaluationError) and with it the task."""
    item = evaluate(template["States"]["Save_Task_Record"]["Arguments"]["Item"], input=PRE_UPGRADE_WRAPPER_INPUT)
    assert {name: value for name, value in item.items() if None in value.values()} == {}
    assert item["starter_input"] == {"S": ""}                 # restart treats it as a legacy row
    assert item["orchestration_timeout"] == {"N": "0"}        # unknown: readers use the platform default


@pytest.mark.parametrize("row,allowed", [
    ({"status": "waiting", "generation": 1, "wrapper_execution_arn": "arn:self"}, True),     # its own retry
    ({"status": "running", "generation": 1, "wrapper_execution_arn": "arn:self"}, False),    # it moved on meanwhile
    ({"status": "stopped", "generation": 1, "wrapper_execution_arn": "arn:old"}, True),      # a restart's new wrapper
    ({"status": "success", "generation": 2, "wrapper_execution_arn": "arn:old"}, False),     # a newer owner holds it
    ({"status": "waiting", "wrapper_execution_arn": "arn:self"}, True),                     # a row from before 1.2
], ids=["retry-waiting", "retry-moved-on", "restart", "superseded", "legacy-row"])
def test_who_may_save_the_task_row(template, row, allowed):
    """The real condition, evaluated by DynamoDB (moto): a save retried after a lost answer must not reset
    a task that started meanwhile (round 3), and only the row's owner may register."""
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    arguments = template["States"]["Save_Task_Record"]["Arguments"]
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        table.put_item(Item={"execution_name": "load-2026-11-12-run", **row})
        try:
            table.put_item(Item={"execution_name": "load-2026-11-12-run", "status": "waiting", "generation": 1},
                           ConditionExpression=arguments["ConditionExpression"],
                           ExpressionAttributeNames=arguments["ExpressionAttributeNames"],
                           ExpressionAttributeValues={":g": 1, ":self": "arn:self", ":waiting": "waiting",
                                                      ":paused": "waiting_paused"})
            saved = True
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
            saved = False
    assert saved is allowed

