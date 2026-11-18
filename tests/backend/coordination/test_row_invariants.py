"""The checks of ADR #137's invariants (row_invariants.py) flag what they should, and only that.

Each test plants one violation in a view of the stack and shows its check
finds it, then a near miss that is legal and shows the check stays quiet.
"""

from datetime import datetime, timedelta, timezone

from row_invariants import (
    RUN_CHECK_EVERY, Answer, Run, RunTaskExecution, Start, Task, View,
    invariant_1, invariant_3, invariant_4, invariant_8, invariant_9, invariant_11, invariant_12, liveness,
)

NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
RUN = "orders-run-2026-11-18-abcd0001"


def active(**kwargs) -> Run:
    fields = {"id": RUN, "status": "active", "token": "t2", "due_at": NOW + timedelta(minutes=5)}
    return Run(**{**fields, **kwargs})


def task(status, key="load", generation=1, **kwargs) -> Task:
    return Task(key=key, run=RUN, status=status, generation=generation, **kwargs)


def view(runs=(), tasks=(), executions=(), starts=(), answers=()) -> View:
    return View(NOW, list(runs), list(tasks), list(executions), list(starts), list(answers))


# ── invariant 1: a task that does not run has no execution and no token ──

def test_1_a_waiting_row_with_a_token_is_flagged():
    found = invariant_1(view([active()], [task("waiting", token_fields=("wait_token",))]))
    assert found == ["invariant 1: load carries wait_token"]


def test_1_run_task_running_for_a_waiting_rows_generation_is_flagged():
    found = invariant_1(view([active()], [task("waiting", generation=2)], [RunTaskExecution("load", 2, running=True)]))
    assert found == ["invariant 1: load is waiting, yet run_task runs for its generation 2"]


def test_1_an_old_generation_still_running_after_a_restart_is_cleanup_not_a_violation():
    assert invariant_1(view([active()], [task("waiting", generation=3)], [RunTaskExecution("load", 2, running=True)])) == []


def test_1_a_started_or_running_row_may_have_its_execution():
    executions = [RunTaskExecution("load", 1, running=True), RunTaskExecution("report", 1, running=True)]
    assert invariant_1(view([active()], [task("deps_ready"), task("running", key="report")], executions)) == []


# ── invariant 3: write first, then start ─────────────────────────────

def test_3_a_start_for_a_row_still_waiting_is_flagged():
    found = invariant_3(view(starts=[Start("load", 1, row_status="waiting", row_generation=1)]))
    assert found == ["invariant 3: run_task started for load generation 1 while the row was waiting under generation 1"]


def test_3_a_start_under_another_generation_is_flagged():
    assert invariant_3(view(starts=[Start("load", 1, row_status="deps_ready", row_generation=2)])) != []


def test_3_a_start_after_deps_ready_under_its_generation_passes():
    assert invariant_3(view(starts=[Start("load", 2, row_status="deps_ready", row_generation=2)])) == []


# ── invariant 4: at most one run_task per row and generation ─────────

def test_4_two_executions_for_one_generation_are_flagged():
    executions = [RunTaskExecution("load", 1, running=False), RunTaskExecution("load", 1, running=True)]
    assert invariant_4(view(executions=executions)) == ["invariant 4: load has 2 run_task executions for generation 1"]


def test_4_one_execution_per_generation_passes():
    executions = [RunTaskExecution("load", 1, running=False), RunTaskExecution("load", 2, running=True)]
    assert invariant_4(view(executions=executions)) == []


# ── invariant 8: a run's end is decided from facts, both ways ─────────

def test_8_every_row_final_and_nothing_left_to_answer_the_token_is_flagged():
    found = invariant_8(view([active(due_at=None)], [task("success", output_recorded=True)]))
    assert found == [f"invariant 8: every row of {RUN} is final, its token is not answered, and nothing will check it"]


def test_8_an_answer_still_due_or_recorded_passes():
    assert invariant_8(view([active()], [task("success")])) == []                          # due: a check answers it
    assert invariant_8(view([active(due_at=None, answered="t2")], [task("success")])) == []


def test_8_an_execution_that_ended_while_a_row_waits_is_flagged_unless_stopped():
    found = invariant_8(view([active(execution_ended=True)], [task("waiting")]))
    assert found == [f"invariant 8: the execution of {RUN} ended while load were not final"]
    assert invariant_8(view([active(execution_ended=True, stop_mark=True)], [task("waiting")])) == []


def test_8_an_answer_sent_while_a_row_was_not_final_is_flagged_unless_stopped():
    answer = Answer(RUN, "t2", unfinished=("load",))
    assert invariant_8(view([active()], [task("success")], answers=[answer])) == [
        f"invariant 8: the token of {RUN} was answered while load were not final"]
    stopped = Answer(RUN, "t2", unfinished=("load",), stop_mark=True)
    assert invariant_8(view([active()], [task("stopped")], answers=[stopped])) == []


def test_8_a_run_still_creating_is_not_judged():
    assert invariant_8(view([active(status="creating", due_at=None)], [task("success")])) == []


# ── invariant 9: every unfinished row is covered by its run row's due time ──

def test_9_a_run_with_a_waiting_row_and_no_due_time_is_flagged():
    assert invariant_9(view([active(due_at=None)], [task("waiting")])) == [f"invariant 9: {RUN} has work left and no due time"]


def test_9_a_due_time_further_than_30_minutes_is_flagged_and_30_minutes_passes():
    late = view([active(due_at=NOW + RUN_CHECK_EVERY + timedelta(seconds=1))], [task("waiting")])
    assert invariant_9(late) != []
    assert invariant_9(view([active(due_at=NOW + RUN_CHECK_EVERY)], [task("waiting")])) == []


def test_9_a_stop_in_progress_or_a_request_needs_a_due_time_too():
    assert invariant_9(view([active(due_at=None, stop_mark=True, answered="t2")], [task("stopped")])) != []
    assert invariant_9(view([active(due_at=None, request_pending=True, answered="t2")], [task("success")])) != []


def test_9_a_finished_and_answered_run_needs_no_due_time():
    assert invariant_9(view([active(due_at=None, answered="t2")], [task("success")])) == []


# ── invariant 11: a row outlives its run ──────────────────────────────

def test_11_a_row_that_expired_while_its_run_goes_on_is_flagged():
    expired = task("waiting", expires_at=NOW - timedelta(seconds=1))
    assert invariant_11(view([active()], [expired])) == [f"invariant 11: load expired while its run {RUN} goes on"]
    assert invariant_11(view([active(expires_at=NOW)], [])) == [f"invariant 11: the run row of {RUN} expired while the run goes on"]


def test_11_rows_of_an_ended_run_may_expire():
    expired = task("success", expires_at=NOW - timedelta(days=1))
    assert invariant_11(view([active(execution_ended=True)], [expired])) == []


# ── invariant 12: no status before its data ───────────────────────────

def test_12_a_success_without_its_output_is_flagged():
    assert invariant_12(view([active()], [task("success")])) == ["invariant 12: load reads success, but its output is not recorded"]


def test_12_a_success_with_its_output_or_a_skip_passes():
    assert invariant_12(view([active()], [task("success", output_recorded=True), task("skipped", key="report")])) == []


# ── liveness at a scenario's end ──────────────────────────────────────

def test_liveness_a_run_left_going_with_nothing_to_wait_for_is_flagged():
    assert liveness(view([active()], [task("waiting")])) == [
        f"liveness: {RUN} neither ended nor is paused, and no row waits for a decision"]


def test_liveness_an_ended_or_paused_run_or_a_decision_with_a_due_time_passes():
    assert liveness(view([active(execution_ended=True)], [task("success")])) == []
    assert liveness(view([active(paused=True)], [task("waiting")])) == []
    assert liveness(view([active()], [task("waiting_decision")])) == []
    assert liveness(view([active(due_at=None)], [task("waiting_decision")])) != []
