"""Canonical execution-status derivation + reconciliation (ADR #112)."""
import pytest

from polyris.constants import (
    derive_execution_status,
    reconcile_execution_status,
    normalize_execution_status,
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
        # Failure outranks everything.
        (["failed"], ExecutionStatus.FAILED),
        (["success", "failed"], ExecutionStatus.FAILED),
        (["failed", "aborted"], ExecutionStatus.FAILED),
        (["running", "failed"], ExecutionStatus.FAILED),
        # Interruption without a genuine failure → aborted.
        (["aborted"], ExecutionStatus.ABORTED),
        (["stopped"], ExecutionStatus.ABORTED),
        (["upstream_failed"], ExecutionStatus.ABORTED),
        (["success", "aborted"], ExecutionStatus.ABORTED),
        (["success", "stopped", "upstream_failed"], ExecutionStatus.ABORTED),
        (["running", "aborted"], ExecutionStatus.ABORTED),
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


class TestReconcileExecutionStatus:
    def test_terminal_base_is_untouched(self):
        assert reconcile_execution_status("failed", "running", False) == "failed"
        assert reconcile_execution_status("aborted", "success", True) == "aborted"
        assert reconcile_execution_status("success", "failed", True) == "success"

    def test_running_without_sfn_stays_running(self):
        assert reconcile_execution_status("running", None, False) == "running"
        assert reconcile_execution_status("running", "running", False) == "running"

    def test_running_adopts_terminal_sfn(self):
        assert reconcile_execution_status("running", "aborted", False) == "aborted"
        assert reconcile_execution_status("running", "success", False) == "success"
        assert reconcile_execution_status("running", "timed_out", False) == "timed_out"

    def test_recovered_when_sfn_failed_but_all_tasks_resolved(self):
        assert reconcile_execution_status("running", "failed", True) == "recovered"
        assert reconcile_execution_status("running", "timed_out", True) == "recovered"

    def test_failed_with_unresolved_tasks_stays_failed(self):
        assert reconcile_execution_status("running", "failed", False) == "failed"


class TestNormalizeSupersedesAdr71:
    def test_sfn_succeeded_maps_to_success(self):
        assert normalize_execution_status("SUCCEEDED") == ExecutionStatus.SUCCESS
        assert normalize_execution_status("success") == ExecutionStatus.SUCCESS
        assert normalize_execution_status("succeeded") == ExecutionStatus.SUCCESS

    def test_stopped_maps_to_aborted(self):
        assert normalize_execution_status("STOPPED") == ExecutionStatus.ABORTED

    def test_canonical_set_contract(self):
        assert "success" in EXECUTION_STATUS_CANONICAL
        assert "succeeded" not in EXECUTION_STATUS_CANONICAL
        assert "stopped" not in EXECUTION_STATUS_CANONICAL
        assert EXECUTION_STATUS_CANONICAL == {
            "running", "success", "failed", "timed_out", "aborted", "recovered",
        }
