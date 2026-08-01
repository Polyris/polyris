"""Tests for routes.tasks._format_task_row and _reconcile_orphaned_tasks.

Regression tests for a real bug found in a code-review pass:
_format_task_row's output dict silently dropped 'wrapper_execution_arn' —
even though it's fetched from DynamoDB (it's part of _TASKS_PROJECTION) and
get_all_tasks feeds _format_task_row's OUTPUT straight into
_reconcile_orphaned_tasks, which needs that exact field to tell "genuinely
orphaned" apart from "task was restarted, its new wrapper is still running".
With the field missing, _reconcile_orphaned_tasks always saw an empty
wrapper_execution_arn, never entered its wrapper-liveness check, and
unconditionally marked every non-terminal task in a failed pipeline
execution as 'aborted' — including tasks whose restart wrapper was
genuinely still running. Users would see "aborted" in the History tasks
feed for a task that was actually alive and progressing.
"""

import pytest

from routes.tasks import _format_task_row, _reconcile_orphaned_tasks


def _raw_item(status="waiting_delay", wrapper_arn="", pipeline_execution="run1",
              pipeline_name="sales"):
    return {
        "execution_name": "extract-2024-01-15-abc12345",
        "task_name": "extract",
        "pipeline_name": pipeline_name,
        "status": status,
        "date": "2024-01-15",
        "started_at": "2024-01-15T08:00:00Z",
        "pipeline_execution": pipeline_execution,
        "wrapper_execution_arn": wrapper_arn,
    }


class TestFormatTaskRowPreservesWrapperArn:
    def test_wrapper_execution_arn_present_in_output(self):
        formatted = _format_task_row(_raw_item(wrapper_arn="arn:aws:states:us-east-1:123456789012:execution:w:1"))
        assert formatted["wrapper_execution_arn"] == "arn:aws:states:us-east-1:123456789012:execution:w:1"

    def test_wrapper_execution_arn_absent_upstream_yields_none(self):
        item = _raw_item()
        del item["wrapper_execution_arn"]
        formatted = _format_task_row(item)
        assert formatted["wrapper_execution_arn"] is None


class TestReconcileOrphanedTasksWrapperLiveness:
    """These tests replace the module-level sfn/pipelines_repo references
    directly (the lazy-proxy shape used throughout console_api makes
    patch.object on individual methods awkward — see routes/pipelines_list.py
    tests for the same pattern)."""

    @pytest.fixture
    def fake_arns(self, monkeypatch):
        import routes.tasks as tasks_module
        monkeypatch.setattr(
            tasks_module.pipelines_repo, "get",
            lambda name: {"sfn_arn": "arn:aws:states:us-east-1:123456789012:stateMachine:sales"},
        )
        return tasks_module

    def test_running_wrapper_is_not_marked_aborted(self, fake_arns, mocker):
        """The exact bug: a restarted task (new wrapper genuinely RUNNING)
        must keep its real status, not be overwritten to 'aborted'."""
        fake_sfn = mocker.MagicMock()
        fake_sfn.describe_execution.side_effect = (
            lambda executionArn: {"status": "RUNNING"} if "wrapper" in executionArn
            else {"status": "FAILED"}
        )
        fake_arns.sfn = fake_sfn

        formatted = _format_task_row(_raw_item(
            wrapper_arn="arn:aws:states:us-east-1:123456789012:execution:wrapper:w1"))
        result = _reconcile_orphaned_tasks([formatted])

        assert result[0]["status"] == "waiting_delay"

    def test_dead_wrapper_is_marked_aborted(self, fake_arns, mocker):
        """Control: no live wrapper -> genuinely orphaned -> aborted."""
        fake_sfn = mocker.MagicMock()
        fake_sfn.describe_execution.return_value = {"status": "FAILED"}
        fake_arns.sfn = fake_sfn

        formatted = _format_task_row(_raw_item(wrapper_arn=""))
        result = _reconcile_orphaned_tasks([formatted])

        assert result[0]["status"] == "aborted"

    def test_healthy_pipeline_execution_leaves_status_untouched(self, fake_arns, mocker):
        """If the pipeline execution itself is still RUNNING (not
        failed/timed_out/aborted), nothing should be reconciled at all."""
        fake_sfn = mocker.MagicMock()
        fake_sfn.describe_execution.return_value = {"status": "RUNNING"}
        fake_arns.sfn = fake_sfn

        formatted = _format_task_row(_raw_item(wrapper_arn=""))
        result = _reconcile_orphaned_tasks([formatted])

        assert result[0]["status"] == "waiting_delay"

    def test_already_terminal_tasks_are_left_alone(self, fake_arns, mocker):
        """Settled tasks never enter the pending group at all — no SFN call
        should even be attempted for them."""
        fake_sfn = mocker.MagicMock()
        fake_arns.sfn = fake_sfn

        formatted = _format_task_row(_raw_item(status="success"))
        result = _reconcile_orphaned_tasks([formatted])

        assert result[0]["status"] == "success"
        fake_sfn.describe_execution.assert_not_called()
