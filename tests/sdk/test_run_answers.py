"""Who tells a pipeline run how its task ended (ADR #135).

A run waits for each task on a token. run_task answers it when the task
succeeds, and on its own failure paths. The failure handler answers it whenever
the wrapper fails, without guessing whether run_task already did. The
reconciler answers it when it aborts a task whose wrapper is gone
(sam/lambdas/coordination/tests). A token that is already closed is fine; any
other error reaches the bell, because the run would stay RUNNING with nothing
to show for it.
"""

from pathlib import Path

import pytest
from sfn_jsonata import NOW, evaluate, load_template, plain

from polyris.coordination.records import warning_item
from polyris.coordination.wake import DEAD_TOKEN_CODES

TEMPLATES = Path(__file__).resolve().parents[2] / "sam" / "sfn_templates"
CLOSED = sorted(f"Sfn.{code}Exception" for code in DEAD_TOKEN_CODES)
ROW = {"execution_name": "load-2026-11-12--2026-11-12-abcdef12", "task_name": "load", "pipeline_name": "orders",
       "pipeline_execution": "orders-run-2026-11-12-abcdef12", "date": "2026-11-12"}
#: Every template state that answers a run's token, and where it goes once the run is answered.
#: An answer that warns itself when it cannot get through to the run.
ANSWERS = [("dependency_wrapper/sfn.tpl.json", "Auto_Skip_Send_Success", "Done"),
           ("helpers/failure_handler/sfn.tpl.json", "Send_Task_Failure", "Failure_Complete"),
           ("helpers/run_task/sfn.tpl.json", "Send_Pipeline_Success", "Done")]
#: An answer the failure handler backs: run_task's failure path ends in Fail_State, and the
#: wrapper's catch runs the failure handler, which answers again or warns.
BACKED = [("helpers/run_task/sfn.tpl.json", "Send_Pipeline_Failure", "Fail_State")]


def _walk(states):
    for name, state in states.items():
        yield name, state
        for branch in state.get("Branches", []) or []:
            yield from _walk(branch["States"])
        for key in ("ItemProcessor", "Iterator"):
            if key in state:
                yield from _walk(state[key]["States"])


def test_every_answer_to_a_run_has_a_role():
    """A new state that answers a run fails here until it is listed: a run whose answer is lost
    stays RUNNING with nothing to show for it, the class ADR #135 closes."""
    found = set()
    for path in TEMPLATES.rglob("sfn.tpl.json"):
        template = str(path.relative_to(TEMPLATES))
        for name, state in _walk(_states(template)):
            if str(state.get("Resource", "")).endswith(("sendTaskSuccess", "sendTaskFailure")) and \
                    "orchestration_token" in str(state.get("Arguments", {}).get("TaskToken", "")):
                found.add((template, name))
    assert found == {(t, s) for t, s, _ in ANSWERS + BACKED}


@pytest.mark.parametrize("template,state,done", ANSWERS + BACKED)
def test_a_closed_token_is_never_retried(template, state, done):
    closed_retry = _states(template)[state]["Retry"][0]
    assert (sorted(closed_retry["ErrorEquals"]), closed_retry["MaxAttempts"]) == (CLOSED, 0)


@pytest.mark.parametrize("template,state,done", BACKED)
def test_a_backed_answer_ends_where_the_failure_handler_takes_over(template, state, done):
    states = _states(template)
    assert states[state]["Next"] == done and all(c["Next"] == done for c in states[state]["Catch"])
    assert states[done]["Type"] == "Fail"


def _states(template):
    return load_template(template, {"tokens_table": "pipeline-tokens"})["States"]


@pytest.mark.parametrize("given,answers", [
    ({"orchestration_token": "t"}, True),
    ({"orchestration_token": "t", "helper_failed": True}, True),   # the old flag no longer silences it
    ({"orchestration_token": ""}, False),
    ({}, False),
])
def test_the_failure_handler_answers_whenever_it_knows_the_token(given, answers):
    [choice] = _states("helpers/failure_handler/sfn.tpl.json")["Check_Orchestration_Token"]["Choices"]
    assert choice["Next"] == "Send_Task_Failure"
    assert bool(evaluate(choice["Condition"], input=given)) is answers


def test_no_template_marks_a_failure_as_answered_already():
    """helper_failed told the failure handler that run_task had answered the run; twice it had not (F16, B1)."""
    assert [p.name for p in TEMPLATES.rglob("*.tpl.json") if "helper_failed" in p.read_text()] == []


@pytest.mark.parametrize("template,state,done", ANSWERS)
def test_a_closed_token_is_final_and_any_other_error_reaches_the_bell(template, state, done):
    states = _states(template)
    answer = states[state]
    closed, other = answer["Catch"][0], answer["Catch"][-1]
    assert (sorted(closed["ErrorEquals"]), closed["Next"]) == (CLOSED, done)
    assert (other["ErrorEquals"], other["Next"]) == (["States.ALL"], "Warn_Run_Left_Running")
    warn = states["Warn_Run_Left_Running"]
    assert warn["Next"] == done and all(c["Next"] == done for c in warn["Catch"])


@pytest.mark.parametrize("template,state,done", ANSWERS)
def test_the_run_warning_has_the_shape_of_warning_item(template, state, done):
    item = _states(template)["Warn_Run_Left_Running"]["Arguments"]["Item"]
    given = {**ROW, "callback_error": {"Error": "Sfn.AccessDeniedException", "Cause": "not authorized"}}
    written = plain(evaluate(item, input=given))
    expected = warning_item(kind="coordination", execution_name=ROW["execution_name"], task_name="load",
                            pipeline_name="orders", pipeline_execution=ROW["pipeline_execution"],
                            task_date="2026-11-12", error=written["error"], now=NOW)
    assert written == expected
    assert "stays RUNNING until you stop it" in written["error"]
    assert "Sfn.AccessDeniedException" in written["error"]
