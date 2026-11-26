"""The harness interpreter itself, on small synthetic state machines.

If the interpreter is wrong, every scenario built on it is wrong, so each
semantic the scenarios rely on is pinned here first.
"""

import itertools
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


def test_a_held_retry_waits_for_its_release_and_then_runs_with_its_count(rt):
    calls = []

    def dies_once(event, context):
        calls.append(event)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"attempt": len(calls)}

    rt.lambdas["dies_once"] = dies_once
    m = rt.add_machine("m", machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
              "Arguments": {"FunctionName": "arn:aws:lambda:us-east-1:1:function:dies_once",
                            "Payload": {"retry": "{% $states.context.State.RetryCount %}"}},
              "Output": "{% $states.result.Payload %}",
              "Retry": [{"ErrorEquals": ["RuntimeError"], "MaxAttempts": 2}], "End": True}}))
    rt.hold_retries(m.arn, "A")
    ex = rt.executions[rt.start_execution(stateMachineArn=m.arn, input="{}")["executionArn"]]
    rt.run()
    assert (len(calls), ex.status) == (1, "RUNNING")          # the retry waits: it comes late
    rt.release_retries(m.arn, "A")
    rt.run()
    assert (calls[1], ex.status, ex.output) == ({"retry": 1}, "SUCCEEDED", {"attempt": 2})


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
              "Assign": {"signal": "{% $states.result.signal %}"},
              "Output": "{% $exists($signal) ? $signal : 'not assigned yet' %}", "End": True},
    }), name="waiter")
    assert ex.status == "RUNNING"
    [helper] = [e for e in rt.executions.values() if e.machine is child]
    token = helper.input["token"]
    rt.send_task_success(taskToken=token, output=json.dumps({"signal": "deps_ready"}))
    rt.run()
    assert (ex.status, ex.output) == ("SUCCEEDED", "not assigned yet")    # Output sees $signal as it was before Assign
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


def test_an_expression_that_evaluates_to_undefined_fails_like_aws():
    """AWS fails the state: JSON cannot represent undefined (D107). The F16 bug of 1.2.0 hid here."""
    rt = Runtime(None, now=lambda: NOW)
    with pytest.raises(StatesError, match="States.QueryEvaluationError.*undefined"):
        rt.expressions.evaluate("{% $states.input.missing %}", states={"input": {}}, variables={})
    with pytest.raises(StatesError, match="States.QueryEvaluationError.*undefined"):
        rt.expressions.evaluate({"Item": {"k": {"S": "{% $states.input.missing %}"}}}, states={"input": {}}, variables={})


def test_null_is_not_undefined_and_objects_drop_undefined_keys():
    rt = Runtime(None, now=lambda: NOW)
    assert rt.expressions.evaluate("{% $states.input.x %}", states={"input": {"x": None}}, variables={}) is None
    assert rt.expressions.evaluate("{% {'a': null, 'b': [null, 1]} %}", states={}, variables={}) == {"a": None, "b": [None, 1]}
    assert rt.expressions.evaluate("{% {'gone': $states.input.missing, 'kept': 1} %}", states={"input": {}}, variables={}) == {"kept": 1}


def test_an_undefined_field_fails_the_state_and_reaches_its_catch(rt):
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::dynamodb:putItem",
              "Arguments": {"TableName": "t", "Item": {"k": {"S": "{% $states.input.missing %}"}}},
              "Catch": [{"ErrorEquals": ["States.ALL"], "Next": "Caught", "Output": "{% $states.errorOutput.Error %}"}],
              "End": True},
        "Caught": {"Type": "Pass", "End": True},
    }))
    assert (ex.status, ex.output) == ("SUCCEEDED", "States.QueryEvaluationError")
    ex = run(rt, machine({"A": {"Type": "Pass", "Output": "{% $states.input.missing %}", "End": True}}), name="uncaught")
    assert (ex.status, ex.error) == ("FAILED", "States.QueryEvaluationError")


@pytest.mark.parametrize("error,names,expected", [
    ("X", ["States.ALL"], True), ("X", ["X"], True), ("X", ["Y"], False),
    ("X", ["States.TaskFailed"], True), ("States.Timeout", ["States.TaskFailed"], False),
])
def test_error_matching(error, names, expected):
    assert matches(error, names) is expected


# ── behaviour observed live in TR-0 (ADR #137, *Probed AWS behaviour*) ─────────

TOKEN_PAYLOAD = {"token": "{% $states.context.Task.Token %}", "retry": "{% $states.context.State.RetryCount %}",
                 "redrive": "{% $states.context.Execution.RedriveCount %}", "entered": "{% $states.context.State.EnteredTime %}"}


def _start_run_machine():
    """A pipeline's shape in 1.3: Start_Run waits on the run's token beside a direct step; a failure ends it after."""
    return machine({
        "Run": {"Type": "Parallel", "Next": "Check", "Branches": [
            {"StartAt": "Start_Run", "States": {
                "Start_Run": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                              "Arguments": {"FunctionName": "start-run", "Payload": TOKEN_PAYLOAD},
                              "Catch": [{"ErrorEquals": ["RunFailed"], "Next": "Record",
                                         "Output": "{% {'error': $states.errorOutput.Error} %}"}],
                              "End": True},
                "Record": {"Type": "Pass", "End": True}}},
            {"StartAt": "Direct", "States": {"Direct": {"Type": "Pass", "Output": {"ok": True}, "End": True}}}]},
        "Check": {"Type": "Choice", "Choices": [{"Condition": "{% $exists($states.input[0].error) %}", "Next": "Failed"}],
                  "Default": "Done"},
        "Failed": {"Type": "Fail", "Error": "RunFailed"},
        "Done": {"Type": "Succeed"}}, start="Run")


def _recording_runtime(calls, now=lambda: NOW):
    return Runtime(None, now=now, lambdas={"start-run": lambda event, context: calls.append(event) or {}})


def test_a_retried_token_state_gets_a_new_token_its_retry_count_and_one_entered_time():
    """TR-0 B1: each attempt brings a new token, RetryCount counts them, EnteredTime stays, the old token is dead."""
    clock, calls = itertools.count(), []

    def start_run(event, context):
        calls.append(event)
        if event["retry"] == 0:
            raise RuntimeError("a transient failure of the first attempt")
        return {}

    rt = Runtime(None, now=lambda: f"2026-11-12T06:00:{next(clock):02d}.000Z", lambdas={"start-run": start_run})
    ex = run(rt, machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke.waitForTaskToken",
                                "Arguments": {"FunctionName": "start-run", "Payload": TOKEN_PAYLOAD},
                                "Retry": [{"ErrorEquals": ["RuntimeError"], "MaxAttempts": 2}], "End": True}}))
    assert [call["retry"] for call in calls] == [0, 1]
    assert calls[0]["entered"] == calls[1]["entered"]
    assert calls[0]["token"] != calls[1]["token"]
    with pytest.raises(Exception, match="TaskTimedOut"):
        rt.send_task_success(taskToken=calls[0]["token"], output="{}")
    rt.send_task_success(taskToken=calls[1]["token"], output="{}")
    rt.run()
    assert ex.status == "SUCCEEDED"


def test_a_redrive_of_a_stopped_run_starts_its_waiting_state_again():
    """TR-0 B4: an aborted run's redrive runs Start_Run again, with RedriveCount 1 and RetryCount 0."""
    calls = []
    rt = _recording_runtime(calls)
    ex = run(rt, _start_run_machine(), name="stopped-run")
    rt.stop_execution(executionArn=ex.arn)
    rt.redrive_execution(executionArn=ex.arn)
    rt.run()
    assert [(call["retry"], call["redrive"]) for call in calls] == [(0, 0), (0, 1)]
    with pytest.raises(Exception, match="TaskTimedOut"):
        rt.send_task_success(taskToken=calls[0]["token"], output="{}")
    rt.send_task_success(taskToken=calls[1]["token"], output="{}")
    rt.run()
    assert ex.status == "SUCCEEDED"


def test_a_redrive_of_a_failed_run_fails_again_without_starting_the_run_again():
    """TR-0 B4: the run fails after its Parallel, so its redrive fails again at once; Restart reruns tasks."""
    calls = []
    rt = _recording_runtime(calls)
    ex = run(rt, _start_run_machine(), name="failed-run")
    rt.send_task_failure(taskToken=calls[0]["token"], error="RunFailed", cause="the run's tasks failed")
    rt.run()
    assert (ex.status, ex.error) == ("FAILED", "RunFailed")
    rt.redrive_execution(executionArn=ex.arn)
    rt.run()
    assert (ex.status, len(calls)) == ("FAILED", 1)


def test_only_a_failed_or_stopped_execution_can_be_redriven(rt):
    ex = run(rt, machine({"A": {"Type": "Pass", "End": True}}))
    with pytest.raises(Exception, match="ExecutionNotRedrivable"):
        rt.redrive_execution(executionArn=ex.arn)


@pytest.mark.parametrize("names", [["States.ALL"], ["States.DataLimitExceeded"]])
def test_a_result_over_256_kb_fails_the_state_and_either_catch_takes_it(names):
    """TR-0 B2: a Lambda result over 256 KB fails the Task; States.ALL and the named error both catch it."""
    rt = Runtime(None, now=lambda: NOW, lambdas={"big": lambda event, context: {"blob": "x" * 300_000}})
    ex = run(rt, machine({
        "A": {"Type": "Task", "Resource": "arn:aws:states:::lambda:invoke", "Arguments": {"FunctionName": "big", "Payload": {}},
              "Catch": [{"ErrorEquals": names, "Next": "Caught", "Output": "{% $states.errorOutput.Error %}"}], "End": True},
        "Caught": {"Type": "Pass", "End": True}}))
    assert (ex.status, ex.output) == ("SUCCEEDED", "States.DataLimitExceeded")


def test_arguments_over_256_kb_fail_the_state(rt):
    ex = run(rt, machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::dynamodb:putItem",
                                "Arguments": {"TableName": "t", "Item": {"k": {"S": "x" * 300_000}}}, "End": True}}))
    assert (ex.status, ex.error) == ("FAILED", "States.DataLimitExceeded")


def test_a_200_kb_item_goes_through_a_dynamodb_task(rt):
    """TR-0 B3: a success transaction with a 200 KB item went through the SDK integration."""
    ex = run(rt, machine({"A": {"Type": "Task", "Resource": "arn:aws:states:::dynamodb:putItem",
                                "Arguments": {"TableName": "t", "Item": {"k": {"S": "k1"}, "blob": {"S": "x" * 200_000}}},
                                "End": True}}))
    assert ex.status == "SUCCEEDED"
