"""How a run ends (ADR #137 §4, ADR #138 §5)."""

import pytest

from polyris.constants import ExecutionStatus
from polyris.coordination import run_end


def test_a_summary_counts_the_rows_per_status():
    assert run_end.summary(["success", "failed", "success"]) == {"success": 2, "failed": 1}


@pytest.mark.parametrize("counts,step_failed,expected", [
    ({"success": 3, "skipped": 1}, False, ExecutionStatus.SUCCESS),
    ({"success": 3, "failed": 1}, False, ExecutionStatus.FAILED),
    ({"success": 3, "upstream_failed": 1, "stopped": 1}, False, ExecutionStatus.ABORTED),
    ({"failed": 1, "stopped": 1}, False, ExecutionStatus.FAILED),         # a failure outranks a stop
    ({"success": 3}, True, ExecutionStatus.FAILED),                        # a failed direct step
    ({"success": 2, "failed": 1, "running": 1}, False, None),             # a row still runs: no answer
    ({"success": 1, "waiting_decision": 1}, False, None),
    ({}, False, None),
    ({"success": 2, "failed": 0}, False, ExecutionStatus.SUCCESS),        # a zero count is no row
])
def test_the_outcome_waits_for_every_row_to_be_final(counts, step_failed, expected):
    assert run_end.outcome(counts, step_failed=step_failed) == expected


def test_a_cleanup_that_succeeded_after_a_failure_does_not_make_the_run_succeed():
    """ADR #138 §5: a run with a final failure fails."""
    assert run_end.outcome({"failed": 1, "success": 1}, step_failed=False) == ExecutionStatus.FAILED


def test_the_cause_names_the_failed_tasks_and_a_failed_step():
    assert run_end.failure_cause(["load", "extract"]) == "2 tasks failed: extract, load"
    assert run_end.failure_cause([], step_failure="notify") == "step notify failed; 0 tasks failed"


def test_the_cause_of_a_large_run_stays_within_step_functions_limit():
    names = [f"task_with_a_long_name_{i:05d}" for i in range(3000)]
    cause = run_end.failure_cause(names)
    assert len(cause) <= run_end.CAUSE_MAX
    listed = cause.split(": ", 1)[1].rsplit(" and ", 1)
    assert listed[1].endswith(" more") and int(listed[1].split()[0]) == 3000 - len(listed[0].split(", "))


def test_the_summarys_condition_keeps_a_newer_count():
    assert run_end.SUMMARY_CONDITION == "attribute_not_exists(#summary_at) OR #summary_at <= :read_at"
    assert run_end.SUMMARY_NAMES == {"#summary": "summary", "#summary_at": "summary_at"}
