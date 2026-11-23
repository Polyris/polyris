"""Canonical execution-status derivation (ADR #112, as ADR #137 §4 amends it)."""
import pytest

from polyris.constants import (
    derive_execution_status,
    ExecutionStatus,
    EXECUTION_STATUS_CANONICAL,
)


class TestDeriveExecutionStatus:
    @pytest.mark.parametrize("task_statuses, expected", [
        # No tasks yet / still in flight → running.
        ([], ExecutionStatus.RUNNING),
        (["running"], ExecutionStatus.RUNNING),
        (["waiting"], ExecutionStatus.RUNNING),
        (["deps_ready", "running"], ExecutionStatus.RUNNING),
        (["running", "success"], ExecutionStatus.RUNNING),
        # Clean success (succeeded/skipped are resolved too).
        (["success"], ExecutionStatus.SUCCESS),
        (["success", "succeeded", "skipped"], ExecutionStatus.SUCCESS),
        (["success", "skipped"], ExecutionStatus.SUCCESS),
        # Once every task is final, a failure outranks everything.
        (["failed"], ExecutionStatus.FAILED),
        (["success", "failed"], ExecutionStatus.FAILED),
        (["failed", "aborted"], ExecutionStatus.FAILED),
        # Interruption without a genuine failure → aborted.
        (["aborted"], ExecutionStatus.ABORTED),
        (["stopped"], ExecutionStatus.ABORTED),
        (["upstream_failed"], ExecutionStatus.ABORTED),
        (["success", "aborted"], ExecutionStatus.ABORTED),
        (["success", "stopped", "upstream_failed"], ExecutionStatus.ABORTED),
        # A failed or stopped task does not end its run while another is not final (ADR #137 §4, D68).
        (["running", "failed"], ExecutionStatus.RUNNING),
        (["waiting", "failed", "success"], ExecutionStatus.RUNNING),
        (["running", "aborted"], ExecutionStatus.RUNNING),
        (["deps_ready", "stopped"], ExecutionStatus.RUNNING),
        (["waiting_decision", "success"], ExecutionStatus.RUNNING),
        (["waiting_paused", "upstream_failed"], ExecutionStatus.RUNNING),
    ])
    def test_derivation(self, task_statuses, expected):
        assert derive_execution_status(task_statuses) == expected

    def test_output_is_always_canonical(self):
        for combo in ([], ["success"], ["failed"], ["aborted"], ["running"], ["stopped"]):
            assert derive_execution_status(combo) in EXECUTION_STATUS_CANONICAL

    def test_accepts_iterables_and_dedupes(self):
        assert derive_execution_status(iter(["success", "success"])) == ExecutionStatus.SUCCESS

    def test_never_emits_stopped_or_succeeded(self):
        # 'stopped' is task-only; 'succeeded' is an input alias, never an output.
        for combo in (["stopped"], ["succeeded"], ["succeeded", "stopped"]):
            assert derive_execution_status(combo) not in ("stopped", "succeeded")


class TestCanonicalSet:
    def test_canonical_set_contract(self):
        assert "success" in EXECUTION_STATUS_CANONICAL
        assert "succeeded" not in EXECUTION_STATUS_CANONICAL
        assert "stopped" not in EXECUTION_STATUS_CANONICAL
        assert EXECUTION_STATUS_CANONICAL == {
            "running", "success", "failed", "aborted", "stale",
        }
