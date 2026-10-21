"""
E2E Tests — ADR #125 retry visibility contract.

Verifies that `retry_attempt` is present on task rows and that
`TASK_RETRY_STARTED` events carry the required fields when retries exist.
"""
import pytest


class TestRetryAttemptFieldContract:
    """GET /api/tasks — retry_attempt shape (ADR #125)."""

    def test_tasks_include_retry_attempt_field(self, api):
        """Every task row must carry retry_attempt (defaults to 0 for pre-ADR rows)."""
        body = api.get("/api/tasks")["body"]
        tasks = body.get("tasks", body) if isinstance(body, dict) else body
        if not tasks:
            pytest.skip("No tasks in deployment")
        for task in tasks[:20]:
            assert "retry_attempt" in task, (
                f"retry_attempt missing on task {task.get('execution_name')}"
            )
            assert isinstance(task["retry_attempt"], int), (
                f"retry_attempt must be int, got {type(task['retry_attempt'])}"
            )
            assert task["retry_attempt"] >= 0

    def test_attempt_field_is_positive_int(self, api):
        """attempt (wrapper generation) must be a positive integer on every task."""
        body = api.get("/api/tasks")["body"]
        tasks = body.get("tasks", body) if isinstance(body, dict) else body
        if not tasks:
            pytest.skip("No tasks in deployment")
        for task in tasks[:20]:
            assert "attempt" in task, (
                f"attempt missing on task {task.get('execution_name')}"
            )
            assert isinstance(task["attempt"], int)
            assert task["attempt"] >= 1


class TestTaskRetryStartedEvent:
    """GET /api/task-events — TASK_RETRY_STARTED shape (ADR #125)."""

    def test_retry_events_carry_both_counters(self, api):
        """TASK_RETRY_STARTED events must have attempt and retry_attempt fields."""
        body = api.get("/api/tasks")["body"]
        tasks = body.get("tasks", body) if isinstance(body, dict) else body
        retried = next((t for t in tasks if t.get("retry_attempt", 0) > 0), None)
        if retried is None:
            pytest.skip("No retried tasks in deployment — skipping TASK_RETRY_STARTED shape check")

        resp = api.get("/api/task-events", params={"name": retried["execution_name"]})
        assert resp["status"] == 200
        events = resp["body"] if isinstance(resp["body"], list) else resp["body"].get("events", [])

        retry_events = [e for e in events if e.get("event_type") == "TASK_RETRY_STARTED"]
        assert retry_events, (
            f"No TASK_RETRY_STARTED events for task {retried['execution_name']} "
            f"(retry_attempt={retried['retry_attempt']})"
        )
        for evt in retry_events:
            assert "attempt" in evt, "TASK_RETRY_STARTED event missing attempt"
            assert "retry_attempt" in evt, "TASK_RETRY_STARTED event missing retry_attempt"
            assert isinstance(evt["retry_attempt"], int)
            assert evt["retry_attempt"] > 0, "TASK_RETRY_STARTED retry_attempt must be > 0"

    def test_task_events_missing_name_returns_4xx(self, api):
        """GET /api/task-events without name param must return 4xx."""
        resp = api.get("/api/task-events")
        assert resp["status"] in (400, 422)

    def test_task_events_unknown_name_returns_empty_list(self, api):
        """Unknown execution_name must return 200 with empty events, not 404."""
        resp = api.get("/api/task-events", params={"name": "no-such-task-xyz-2099-01-01"})
        assert resp["status"] == 200
        events = resp["body"] if isinstance(resp["body"], list) else resp["body"].get("events", [])
        assert events == []
