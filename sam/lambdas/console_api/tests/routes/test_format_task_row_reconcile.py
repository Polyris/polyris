"""Tests for routes.tasks._format_task_row.

Regression tests for a real bug found in a code-review pass: _format_task_row's
output dict silently dropped 'wrapper_execution_arn', which a 1.2 row still
carries. (The read-time marking of a failed run's tasks that read it went with
ADR #137 §5: the console asks Step Functions nothing about a run.)
"""

from routes.tasks import _format_task_row


def _raw_item(status="waiting", wrapper_arn="", pipeline_execution="run1",
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
