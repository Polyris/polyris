"""Contract tests for the pause waiter (ADR #130 §8 step 2, §9).

pause_waiter saves the task token of run_task's pause wait and puts the row
in the attention index, so the sweeper can release a task that a resume
missed; a token it cannot save fails the pause wait with the cause. Its
expressions are evaluated with jsonata-python and compared with the Python
the sweeper uses; tests/backend/coordination runs the template.
"""

import pytest
from sfn_jsonata import NOW, evaluate, load_template

from polyris.coordination import attention, wake
from polyris.coordination.times import format_utc


@pytest.fixture(scope="module")
def states() -> dict:
    return load_template("helpers/pause_waiter/sfn.tpl.json")["States"]


@pytest.fixture(scope="module")
def update(states) -> dict:
    return states["Save_Pause_Token"]["Arguments"]


def _value(update: dict, placeholder: str, states_input: dict) -> object:
    (kind, expression), = update["ExpressionAttributeValues"][placeholder].items()
    return evaluate(expression, input=states_input) if kind == "S" else expression


def test_the_row_is_held_with_its_token_and_a_paused_entry(update):
    expression = update["UpdateExpression"]
    for assignment in ("#s = :status", "pause_token = :token", f"{attention.KEY} = :attention",
                       f"{attention.DUE} = :due", "attempts = :zero"):
        assert assignment in expression, assignment
    assert update["ExpressionAttributeValues"][":status"] == {"S": "waiting_paused"}
    assert update["ExpressionAttributeValues"][":zero"] == {"N": "0"}


@pytest.mark.parametrize("name", ["load-2026-11-12--2026-11-12-abcdef12", "a", "x" * 200])
def test_the_paused_shard_matches_python(update, name):
    assert _value(update, ":attention", {"execution_name": name}) == attention.attention(attention.PAUSED, name)


def test_the_first_check_is_due_when_python_says(update):
    assert _value(update, ":due", {}) == format_utc(NOW + attention.FIRST_DUE)


def test_a_token_that_cannot_be_saved_fails_the_pause_wait_with_the_cause(states):
    [catch] = states["Save_Pause_Token"]["Catch"]
    assert (catch["ErrorEquals"], catch["Next"]) == (["States.ALL"], "Fail_Pause_Wait")
    carried = evaluate(catch["Output"], input={"execution_name": "load-x", "pause_token": "pause-1"},
                       error_output={"Error": "DynamoDB.AmazonDynamoDBException", "Cause": "AccessDenied"})

    fail = states["Fail_Pause_Wait"]
    arguments = evaluate(fail["Arguments"], input=carried)

    assert fail["Resource"] == "arn:aws:states:::aws-sdk:sfn:sendTaskFailure" and fail["End"] is True
    assert (arguments["TaskToken"], arguments["Error"]) == ("pause-1", wake.COORDINATION_ERROR)
    assert arguments["Cause"].startswith("The task could not enter its pause wait, so it did not start")
    assert "AccessDenied" in arguments["Cause"]
