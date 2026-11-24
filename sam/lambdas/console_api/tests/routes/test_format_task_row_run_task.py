"""Tests for routes.tasks._format_task_row: the run_task execution a task row names.

A row names its latest run_task execution (``task_row.RUN_TASK_ARN``), which the
task modal links to in the AWS console; 1.2's wrapper ARN went with the wrapper
(ADR #137, What is removed).
"""

from polyris.coordination import task_row
from routes.tasks import _format_task_row


def _raw_item(run_task_arn=None):
    return {
        "execution_name": "extract-2024-01-15-abc12345",
        "task_name": "extract",
        "pipeline_name": "sales",
        "status": "running",
        "date": "2024-01-15",
        "started_at": "2024-01-15T08:00:00Z",
        "pipeline_execution": "run1",
        **({task_row.RUN_TASK_ARN: run_task_arn} if run_task_arn else {}),
    }


def test_the_row_s_run_task_execution_is_in_the_output():
    arn = "arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-run-task-helper:extract-x"
    assert _format_task_row(_raw_item(arn))["run_task_arn"] == arn


def test_a_row_without_one_yields_none_and_no_wrapper_field():
    formatted = _format_task_row(_raw_item())
    assert formatted["run_task_arn"] is None and "wrapper_execution_arn" not in formatted
