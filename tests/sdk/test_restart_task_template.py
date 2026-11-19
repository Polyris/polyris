"""Restart replays the input the task was first started with (ADR #130 §6.5, B-R1).

A 1.2 pipeline state machine started a wrapper with the input its generator
built; registration saved it as `starter_input`. A restart that rebuilt the
input from a few saved fields lost `wait_for`, so a restarted task ran without
waiting for its assets. Both inputs are evaluated here as Step Functions would.
"""
import json
from typing import Any, Dict

import pytest

from polyris.coordination.records import warning_item
from sfn_jsonata import NOW, evaluate, load_template, plain

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:user-load"
RUN = "orders-run-2026-11-12-abcdef12"
EXECUTION = "load-2026-11-12--2026-11-12-abcdef12"
#: Fields a restart sets for the new attempt instead of replaying them.
RESTART_FIELDS = {"token", "execution_name_override", "task_run_id", "attempt", "task_config", "outlets"}


@pytest.fixture(scope="module")
def restart():
    return load_template("helpers/restart_task/sfn.tpl.json", {"tokens_table": "tokens"})["States"]


#: What a 1.2 pipeline passed when it started the wrapper of `load` (one_success after `extract`,
#: wait_for lake/prices within an hour, outlet lake/sales, retries=2), frozen from the last
#: generator that started wrappers (ededcc9). The restart helper only restarts rows a 1.2 run
#: started: from 1.3 a run's tasks are rows that Start_Run writes (ADR #137 §1).
STARTER_1_2: Dict[str, Any] = (
    {'PARTITION_ARG': '2026-11-12',
     '_suppress_asset_event': False,
     'cascade_all': False,
     'cross_account_role': 'same',
     'current_date': '2026-11-12',
     'date': '2026-11-12',
     'dependencies': ['extract'],
     'orchestration_timeout': 86400,
     'outlets': [{'granularity': 'daily', 'name': 'lake/sales', 'uri': ''}],
     'pipeline_execution': 'orders-run-2026-11-12-abcdef12',
     'pipeline_execution_short': '-2026-11-12-abcdef12',
     'pipeline_name': 'orders',
     'skip_tasks': [],
     'task_arn': 'arn:aws:states:us-east-1:123456789012:stateMachine:user-load',
     'task_config': {'retries': 2, 'retry_delay': 300},
     'task_name': 'load',
     'task_type': 'sfn',
     'token': 'pipeline-token',
     'trigger_rule': 'one_success',
     'triggered_by': 'unknown',
     'variables': {'current_date': '2026-11-12', 'region': 'eu'},
     'wait_before': 0,
     'wait_for': [{'asset_name': 'lake/prices', 'freshness_hours': 1}]}
)


@pytest.fixture(scope="module")
def starter_input() -> Dict[str, Any]:
    started = json.loads(json.dumps(STARTER_1_2))
    assert started["wait_for"], "the fixture must exercise wait_for"
    return started


def _row(starter: Dict[str, Any] = None) -> Dict[str, Any]:
    """The task row as restart_task reads it (DynamoDB-typed)."""
    row = {
        "execution_name": {"S": EXECUTION}, "task_name": {"S": "load"}, "task_arn": {"S": ARN},
        "pipeline_name": {"S": "orders"}, "pipeline_execution": {"S": RUN},
        "pipeline_execution_short": {"S": RUN[-20:]}, "date": {"S": "2026-11-12"},
        "orchestration_token": {"S": "pipeline-token"}, "task_run_id": {"S": f"{EXECUTION}-1"},
        "attempt": {"N": "1"}, "dependencies": {"S": json.dumps(["extract"])},
    }
    if starter is not None:
        row["starter_input"] = {"S": json.dumps(starter)}
    return row


def _restart_input(restart, row, *, task_config=None, outlets=None) -> Dict[str, Any]:
    console = {"item": row, "execution_name": EXECUTION,
               "task_config": task_config or {}, "outlets": outlets or []}
    return evaluate(restart["Start_New_Wrapper"]["Arguments"]["Input"], input=console)


def test_restart_replays_every_field_the_pipeline_started_the_wrapper_with(restart, starter_input):
    replayed = _restart_input(restart, _row(starter_input))

    assert {k: replayed[k] for k in starter_input if k not in RESTART_FIELDS} == \
           {k: v for k, v in starter_input.items() if k not in RESTART_FIELDS}
    assert replayed["wait_for"] == starter_input["wait_for"]


def test_restart_sets_the_fields_of_the_new_attempt(restart, starter_input):
    replayed = _restart_input(restart, _row(starter_input), task_config={"retries": 5}, outlets=[{"name": "lake/sales"}])

    assert replayed["execution_name_override"] == EXECUTION
    assert replayed["attempt"] == 2
    assert replayed["token"] == "pipeline-token"
    assert replayed["task_config"] == {"retries": 5}          # the current deploy's, when the console found it
    assert replayed["outlets"] == [{"name": "lake/sales"}]


def test_without_a_current_deploy_the_original_task_config_and_outlets_stay(restart, starter_input):
    replayed = _restart_input(restart, _row(starter_input))

    assert replayed["task_config"] == starter_input["task_config"]
    assert replayed["outlets"] == starter_input["outlets"]


def test_a_row_without_starter_input_restarts_from_its_saved_fields_with_a_warning(restart, starter_input):
    assert restart["Hand_Over_Ownership"]["Next"] == "Check_Starter_Input"
    assert restart["Check_Starter_Input"]["Default"] == "Warn_Restart_Without_Starter_Input"
    assert restart["Warn_Restart_Without_Starter_Input"]["Next"] == "Start_New_Wrapper"
    assert all(c["Next"] == "Start_New_Wrapper" for c in restart["Warn_Restart_Without_Starter_Input"]["Catch"])

    row = _row()
    row["wait_for"] = {"S": json.dumps(starter_input["wait_for"])}     # 1.1.4's registration saved it too
    replayed = _restart_input(restart, row)

    assert (replayed["task_name"], replayed["dependencies"], replayed["date"]) == ("load", ["extract"], "2026-11-12")
    # Without it a restarted pre-upgrade task ran at once, without its assets (external review, round 2).
    assert replayed["wait_for"] == starter_input["wait_for"]
    assert _restart_input(restart, _row())["wait_for"] == []


def test_the_restart_warning_has_the_shape_of_warning_item(restart):
    console = {"item": _row(), "execution_name": EXECUTION}
    written = plain(evaluate(restart["Warn_Restart_Without_Starter_Input"]["Arguments"]["Item"], input=console))
    expected = warning_item(kind="coordination", execution_name=EXECUTION, task_name="load", pipeline_name="orders",
                            pipeline_execution=RUN, task_date="2026-11-12", error=written["error"], now=NOW)
    assert written == expected


def test_an_empty_starter_input_is_a_legacy_row_too(restart):
    """A pre-upgrade wrapper that registered again saved '' (registration's Save_Task_Record)."""
    row = _row()
    row["starter_input"] = {"S": ""}
    [choice] = restart["Check_Starter_Input"]["Choices"]

    assert evaluate(choice["Condition"], input={"item": row}) is False
    replayed = _restart_input(restart, row)
    assert (replayed["task_name"], replayed["dependencies"], replayed["date"]) == ("load", ["extract"], "2026-11-12")


@pytest.mark.parametrize("state", ["Hand_Over_Ownership", "Start_New_Wrapper"])
def test_a_restart_that_fails_says_so_in_the_bell_and_fails(restart, state):
    """Both run after the old wrapper was stopped: a failure there left no trace while the console
    answered "Task restarted" (external review, round 2)."""
    [catch] = restart[state]["Catch"]
    assert (catch["ErrorEquals"], catch["Next"]) == (["States.ALL"], "Warn_Restart_Failed")
    warn = restart["Warn_Restart_Failed"]
    assert warn["Next"] == "Restart_Failed" and all(c["Next"] == "Restart_Failed" for c in warn["Catch"])
    assert restart["Restart_Failed"]["Type"] == "Fail"


def test_the_failed_restart_warning_has_the_shape_of_warning_item(restart):
    console = {"item": _row(), "execution_name": EXECUTION,
               "restart_error": {"Error": "StepFunctions.ExecutionLimitExceeded", "Cause": "too many"}}
    written = plain(evaluate(restart["Warn_Restart_Failed"]["Arguments"]["Item"], input=console))
    expected = warning_item(kind="coordination", execution_name=EXECUTION, task_name="load", pipeline_name="orders",
                            pipeline_execution=RUN, task_date="2026-11-12", error=written["error"], now=NOW)
    assert written == expected
    assert "restart could not start the task" in written["error"]
    assert "StepFunctions.ExecutionLimitExceeded" in written["error"] and "Restart it again" in written["error"]
