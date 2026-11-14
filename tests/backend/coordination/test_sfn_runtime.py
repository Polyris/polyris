"""The harness interpreter itself, on small synthetic state machines.

If the interpreter is wrong, every scenario built on it is wrong, so each
semantic the scenarios rely on is pinned here first.
"""

import json

import boto3
import pytest
from moto import mock_aws

from sfn_runtime import Runtime, StatesError, matches

NOW = "2026-11-12T06:00:00.000Z"


def machine(states, start="A"):
    return {"QueryLanguage": "JSONata", "StartAt": start, "States": states}


@pytest.fixture
def ddb():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="t", BillingMode="PAY_PER_REQUEST",
                            KeySchema=[{"AttributeName": "k", "KeyType": "HASH"}],
                            AttributeDefinitions=[{"AttributeName": "k", "AttributeType": "S"}])
        yield client


@pytest.fixture
def rt(ddb):
    return Runtime(ddb, now=lambda: NOW)


def run(rt, definition, payload=None, name="m", **kwargs):
    m = rt.add_machine(name, definition, **kwargs)
    arn = rt.start_execution(stateMachineArn=m.arn, input=json.dumps(payload or {}))["executionArn"]
    rt.run()
    return rt.executions[arn]


def test_pass_output_assign_and_variables(rt):
    ex = run(rt, machine({
        "A": {"Type": "Pass", "Assign": {"x": "{% $states.input.n + 1 %}"}, "Next": "B"},
        "B": {"Type": "Pass", "Output": "{% {'x': $x, 'n': $states.input.n} %}", "End": True},
    }), {"n": 1})
    assert (ex.status, ex.output) == ("SUCCEEDED", {"x": 2, "n": 1})


def test_output_and_assign_see_the_variables_as_they_were(rt):
    ex = run(rt, machine({
        "A": {"Type": "Pass", "Assign": {"v": 1}, "Next": "B"},
        "B": {"Type": "Pass", "Assign": {"v": 2}, "Output": "{% $v %}", "End": True},
    }))
    assert ex.output == 1


def test_choice_and_default(rt):
    states = {
        "A": {"Type": "Choice", "Choices": [{"Condition": "{% $states.input.n > 1 %}", "Next": "Big"}], "Default": "Small"},
        "Big": {"Type": "Succeed", "Output": "big"},
        "Small": {"Type": "Succeed", "Output": "small"},
    }
    assert run(rt, machine(states), {"n": 2}, name="c1").output == "big"
    assert run(rt, machine(states), {"n": 0}, name="c2").output == "small"


def test_dynamodb_writes_reach_moto_and_errors_are_named(rt, ddb):
    put = {"TableName": "t", "Item": {"k": {"S": "{% $states.input.k %}"}},
           "ConditionExpression": "attribute_not_exists(k)"}
    states = {
        "A": {"Type": "Task", "Resource": "arn:aws:states:::dynamodb:putItem", "Arguments": put,
              "Catch": [{"ErrorEquals": ["DynamoDB.ConditionalCheckFailedException"],
                         "Output": "{% $states.errorOutput.Error %}", "Next": "Exists"}], "End": True},
        "Exists": {"Type": "Succeed"},
    }
    assert run(rt, machine(states), {"k": "a"}, name="d1").status == "SUCCEEDED"
    assert ddb.get_item(TableName="t", Key={"k": {"S": "a"}})["Item"] == {"k": {"S": "a"}}
    assert run(rt, machine(states), {"k": "a"}, name="d2").output == "DynamoDB.ConditionalCheckFailedException"


def test_retry_then_catch(rt):
    calls = []

    def flaky(event, context):
        calls.append(event)
        raise RuntimeError("boom")

    rt.lambdas["flaky"] = flaky
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
              "Arguments": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:flaky", "Payload": {"a": 1}},
              "Retry": [{"ErrorEquals": ["RuntimeError"], "MaxAttempts": 2}],
              "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Caught"}], "End": True},
        "Caught": {"Type": "Pass", "End": True},
    }))
    assert len(calls) == 3 and ex.output["Error"] == "RuntimeError"


def test_an_uncaught_error_fails_the_execution(rt):
    ex = run(rt, machine({"A": {"Type": "Fail", "Error": "Bad", "Cause": "{% 'because ' & $states.input.why %}"}}),
             {"why": "x"})
    assert (ex.status, ex.error, ex.cause) == ("FAILED", "Bad", "because x")


def test_parallel_branches_see_outer_variables_and_results_keep_order(rt):
    ex = run(rt, machine({
        "S": {"Type": "Pass", "Assign": {"v": "two"}, "Next": "A"},
        "A": {"Type": "Parallel", "Branches": [
            {"StartAt": "P1", "States": {"P1": {"Type": "Pass", "Output": 1, "End": True}}},
            {"StartAt": "P2", "States": {"P2": {"Type": "Pass", "Output": "{% $v %}", "End": True}}},
        ], "Next": "M"},
        "M": {"Type": "Map", "Items": "{% [1, 2, 3] %}",
              "ItemSelector": "{% {'i': $states.context.Map.Item.Index, 'v': $states.context.Map.Item.Value} %}",
              "ItemProcessor": {"StartAt": "X", "States": {
                  "X": {"Type": "Pass", "Output": "{% $states.input.v * 10 + $states.input.i %}", "End": True}}},
              "Output": "{% {'map': $states.result, 'par': $states.input} %}", "End": True},
    }, start="S"))
    assert ex.output == {"map": [10, 21, 32], "par": [1, "two"]}


def test_a_failing_branch_fails_the_parallel_and_voids_sibling_tokens(rt):
    child = rt.add_machine("child", machine({"A": {"Type": "Pass", "End": True}}))
    ex = run(rt, machine({
        "A": {"Type": "Parallel", "Branches": [
            {"StartAt": "W", "States": {"W": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
                                               "Arguments": {"StateMachineArn": child.arn, "Input": {"t": "{% $states.context.Task.Token %}"}},
                                               "End": True}}},
            {"StartAt": "F", "States": {"F": {"Type": "Fail", "Error": "Nope"}}},
        ], "End": True},
    }), name="parent")
    assert (ex.status, ex.error) == ("FAILED", "Nope")
    assert not rt.tokens


def test_task_tokens_resume_with_the_sent_output_and_die_after_use(rt):
    child = rt.add_machine("helper", machine({"A": {"Type": "Pass", "End": True}}))
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
              "Arguments": {"StateMachineArn": child.arn, "Input": {"token": "{% $states.context.Task.Token %}"}},
              "Assign": {"signal": "{% $states.result.signal %}"}, "Output": "{% $signal %}", "End": True},
    }), name="waiter")
    assert ex.status == "RUNNING"
    [helper] = [e for e in rt.executions.values() if e.machine is child]
    token = helper.input["token"]
    rt.send_task_success(taskToken=token, output=json.dumps({"signal": "deps_ready"}))
    rt.run()
    assert (ex.status, ex.output) == ("SUCCEEDED", None)    # Output sees $signal as it was before Assign
    with pytest.raises(Exception, match="TaskTimedOut"):
        rt.send_task_success(taskToken=token, output="{}")


def test_a_lambda_may_send_its_token_before_returning(rt):
    rt.lambdas["echo"] = lambda event, context: rt.send_task_success(taskToken=event["token"], output='{"ok": true}')
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
              "Arguments": {"FunctionName": "echo", "Payload": {"token": "{% $states.context.Task.Token %}"}},
              "End": True},
    }))
    assert (ex.status, ex.output) == ("SUCCEEDED", {"ok": True})


def test_send_task_failure_and_timeouts_reach_catch(rt):
    child = rt.add_machine("h", machine({"A": {"Type": "Pass", "End": True}}))
    definition = machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
              "Arguments": {"StateMachineArn": child.arn, "Input": {"token": "{% $states.context.Task.Token %}"}},
              "Catch": [{"ErrorEquals": ["States.ALL"], "Output": "{% $states.errorOutput %}", "Next": "C"}], "End": True},
        "C": {"Type": "Pass", "End": True},
    })
    failed = run(rt, definition, name="f1")
    rt.send_task_failure(taskToken=next(iter(rt.tokens)), error="CoordinationError", cause="why")
    rt.run()
    assert failed.output == {"Error": "CoordinationError", "Cause": "why"}
    timed = run(rt, definition, name="f2")
    rt.time_out(next(iter(rt.tokens)))
    rt.run()
    assert timed.output["Error"] == "States.Timeout"


def test_sync_children_and_express_sync_executions(rt):
    child = rt.add_machine("child", machine({"A": {"Type": "Pass", "Output": "{% $states.input.n * 2 %}", "End": True}}))
    express = rt.add_machine("express", machine({"A": {"Type": "Pass", "Output": "{% 'e' %}", "End": True}}), type="EXPRESS")
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.sync:2",
              "Arguments": {"StateMachineArn": child.arn, "Input": {"n": 21}}, "Output": "{% $states.result.Output %}", "Next": "B"},
        "B": {"Type": "Task", "Resource": "arn:aws:states:::aws-sdk:sfn:startSyncExecution",
              "Arguments": {"StateMachineArn": express.arn, "Input": {}},
              "Output": "{% {'child': $states.input, 'express': $parse($states.result.Output)} %}", "End": True},
    }))
    assert ex.output == {"child": 42, "express": "e"}


def test_stopping_an_execution_voids_its_tokens_and_fails_its_sync_parent(rt):
    child = rt.add_machine("c", machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.waitForTaskToken",
                                              "Arguments": {"StateMachineArn": "arn:aws:states:us-east-1:123456789012:stateMachine:c2",
                                                            "Input": {"t": "{% $states.context.Task.Token %}"}}, "End": True}}))
    rt.add_machine("c2", machine({"A": {"Type": "Pass", "End": True}}))
    parent = run(rt, machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::states:startExecution.sync:2",
                                    "Arguments": {"StateMachineArn": child.arn, "Input": {}}, "End": True}}), name="p")
    [child_ex] = [e for e in rt.executions.values() if e.machine is child]
    rt.stop_execution(executionArn=child_ex.arn)
    rt.run()
    assert (child_ex.status, parent.status, parent.error) == ("ABORTED", "FAILED", "States.TaskFailed")
    assert not rt.tokens
    assert rt.describe_execution(executionArn=child_ex.arn)["status"] == "ABORTED"


def test_external_jobs_wait_for_the_test(rt):
    ex = run(rt, machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::glue:startJobRun.sync",
                                "Arguments": {"JobName": "etl"}, "Output": "{% $states.result.JobRunId %}", "End": True}}))
    [job] = rt.jobs.values()
    assert job.arguments == {"JobName": "etl"} and ex.status == "RUNNING"
    rt.finish_job(job.id, output={"JobRunId": "jr-1"})
    rt.run()
    assert ex.output == "jr-1"


def test_start_execution_is_idempotent_only_for_the_same_running_input(rt):
    m = rt.add_machine("idem", machine({"A": {"Type": "Wait", "Seconds": 1, "Next": "B"},
                                        "B": {"Type": "Task", "Resource": "arn:aws:states:::glue:startJobRun.sync",
                                              "Arguments": {}, "End": True}}))
    first = rt.start_execution(stateMachineArn=m.arn, name="n", input='{"a": 1}')
    assert rt.start_execution(stateMachineArn=m.arn, name="n", input='{"a": 1}') == first
    with pytest.raises(Exception, match="ExecutionAlreadyExists"):
        rt.start_execution(stateMachineArn=m.arn, name="n", input='{"a": 2}')


def test_aws_functions_parse_hash_now():
    rt = Runtime(None, now=lambda: NOW)
    value = rt.expressions.evaluate(
        "{% {'p': $parse('[1]'), 'h': $substring($hash('x', 'SHA-256'), 0, 1), 'n': $now()} %}", states={}, variables={})
    import hashlib
    assert value == {"p": [1], "h": hashlib.sha256(b"x").hexdigest()[0], "n": NOW}


def test_evaluation_errors_are_query_evaluation_errors():
    rt = Runtime(None, now=lambda: NOW)
    with pytest.raises(StatesError, match="States.QueryEvaluationError"):
        rt.expressions.evaluate("{% $notAFunction() %}", states={}, variables={})


@pytest.mark.parametrize("error,names,expected", [
    ("X", ["States.ALL"], True), ("X", ["X"], True), ("X", ["Y"], False),
    ("X", ["States.TaskFailed"], True), ("States.Timeout", ["States.TaskFailed"], False),
])
def test_error_matching(error, names, expected):
    assert matches(error, names) is expected
