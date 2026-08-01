"""
E2E Tests — Tasks, Executions, Notifications endpoints.

Read-only queries. Tests that mutate state are marked @pytest.mark.write.
"""


# =============================================================================
# Tasks
# =============================================================================

class TestListTasks:
    """GET /api/tasks"""

    def test_returns_200(self, api):
        resp = api.get("/api/tasks")
        assert resp["status"] == 200

    def test_returns_list(self, api):
        resp = api.get("/api/tasks")
        body = resp["body"]
        tasks = body if isinstance(body, list) else body.get("tasks", [])
        assert isinstance(tasks, list)

    def test_answers_with_a_cursor_field(self, api):
        """The feed pages on `next` (a started_at, or null when nothing older
        exists) — the UI reads it to know whether "show older" means anything."""
        body = api.get("/api/tasks")["body"]
        assert "next" in body
        assert body["next"] is None or isinstance(body["next"], str)

    def test_page_is_never_longer_than_the_limit(self, api):
        body = api.get("/api/tasks?limit=1")["body"]
        assert len(body["tasks"]) <= 1
        assert body["count"] == len(body["tasks"])

    def test_before_returns_only_older_rows(self, api):
        """Live check of the cursor's meaning against real row timestamps."""
        cursor = "2999-01-01T00:00:00Z"
        body = api.get(f"/api/tasks?before={cursor}")["body"]
        assert all((t.get("started_at") or "") < cursor for t in body["tasks"])

    def test_a_future_cursor_is_the_same_as_no_cursor(self, api):
        first = api.get("/api/tasks?limit=5")["body"]
        cursored = api.get("/api/tasks?limit=5&before=2999-01-01T00:00:00Z")["body"]
        assert [t["execution_name"] for t in cursored["tasks"]] == \
               [t["execution_name"] for t in first["tasks"]]

    def test_garbage_cursor_does_not_500(self, api):
        """A bad query param costs the cursor, not the feed."""
        assert api.get("/api/tasks?before=not-a-timestamp")["status"] == 200


class TestTaskConfig:
    """GET /api/task-config"""

    def test_requires_params(self, api):
        resp = api.get("/api/task-config")
        assert resp["status"] in (400, 422)


class TestTaskEvents:
    """GET /api/task-events"""

    def test_requires_params(self, api):
        resp = api.get("/api/task-events")
        assert resp["status"] in (400, 422)


# =============================================================================
# Executions
# =============================================================================

class TestListRuns:
    """GET /api/runs"""

    def test_returns_200(self, api):
        resp = api.get("/api/runs")
        assert resp["status"] == 200

    def test_returns_list(self, api):
        resp = api.get("/api/runs")
        body = resp["body"]
        runs = body if isinstance(body, list) else body.get("runs", body.get("executions", []))
        assert isinstance(runs, list)

    def test_answers_with_a_cursor_field(self, api):
        body = api.get("/api/runs")["body"]
        assert "next" in body
        assert body["next"] is None or isinstance(body["next"], str)

    def test_page_is_never_longer_than_the_limit(self, api):
        body = api.get("/api/runs?limit=1")["body"]
        assert len(body["runs"]) <= 1
        assert body["count"] == len(body["runs"])

    def test_before_returns_only_older_rows(self, api):
        cursor = "2999-01-01T00:00:00Z"
        body = api.get(f"/api/runs?before={cursor}")["body"]
        assert all((r.get("started_at") or "") < cursor for r in body["runs"])

    def test_paging_never_repeats_a_row(self, api):
        """Walk the feed a page at a time against real data: `next` must always move
        forward, and no run may show up on two pages. Skipped-in-effect on an empty
        deployment, where the first page already exhausts the feed."""
        seen, cursor = [], None
        for _ in range(5):
            query = "/api/runs?limit=2" + (f"&before={cursor}" if cursor else "")
            body = api.get(query)["body"]
            seen.extend(r.get("pipeline_execution") or r.get("backfill_id") for r in body["runs"])
            cursor = body["next"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen))

    def test_garbage_cursor_does_not_500(self, api):
        assert api.get("/api/runs?before=not-a-timestamp")["status"] == 200


class TestExecutionChildren:
    """GET /api/execution-children"""

    def test_requires_params(self, api):
        resp = api.get("/api/execution-children")
        assert resp["status"] in (400, 422)


class TestExecutionParent:
    """GET /api/execution-parent"""

    def test_requires_params(self, api):
        resp = api.get("/api/execution-parent")
        assert resp["status"] in (400, 422)


# =============================================================================
# Notifications
# =============================================================================

class TestNotifications:
    """GET /api/notifications"""

    def test_returns_200(self, api):
        resp = api.get("/api/notifications")
        assert resp["status"] == 200

    def test_returns_list(self, api):
        resp = api.get("/api/notifications")
        body = resp["body"]
        notifs = body if isinstance(body, list) else body.get("notifications", [])
        assert isinstance(notifs, list)


# =============================================================================
# Error handling — unknown routes
# =============================================================================

class TestUnknownRoutes:
    """Verify API returns proper errors for unknown paths."""

    def test_unknown_get_returns_error(self, api):
        resp = api.get("/api/this-does-not-exist")
        # API Gateway returns 404 or 403 for unknown routes
        assert resp["status"] in (403, 404)

    def test_unknown_post_returns_error(self, api):
        resp = api.post("/api/this-does-not-exist")
        assert resp["status"] in (403, 404)
