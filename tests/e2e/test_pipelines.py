"""
E2E Tests — Pipeline endpoints.

Split into read-only tests (safe to run anytime) and write tests (trigger executions).
Write tests are marked with @pytest.mark.write so you can skip them: pytest -m "not write"
"""
import pytest


# =============================================================================
# Read-only pipeline endpoints
# =============================================================================

class TestListPipelines:
    """GET /api/pipelines"""

    def test_returns_200(self, api):
        resp = api.get("/api/pipelines")
        assert resp["status"] == 200

    def test_returns_list(self, api):
        resp = api.get("/api/pipelines")
        body = resp["body"]
        # Response is either a list or {"pipelines": [...]}
        pipelines = body if isinstance(body, list) else body.get("pipelines", [])
        assert isinstance(pipelines, list)

    def test_each_pipeline_has_name(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        for p in registered_pipelines:
            pipeline = p if isinstance(p, dict) else {"name": p}
            assert "name" in pipeline or isinstance(p, str)


class TestPipelineStatus:
    """GET /api/pipeline-status?name=..."""

    def test_requires_name_param(self, api):
        resp = api.get("/api/pipeline-status")
        assert resp["status"] in (400, 422)

    def test_unknown_pipeline_returns_404(self, api):
        resp = api.get("/api/pipeline-status", params={"name": "__nonexistent_e2e_test__"})
        assert resp["status"] in (404, 400)

    def test_valid_pipeline_returns_200(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-status", params={"name": name})
        assert resp["status"] == 200


class TestPipelineExecutions:
    """GET /api/pipeline-executions?name=..."""

    def test_requires_name_param(self, api):
        resp = api.get("/api/pipeline-executions")
        assert resp["status"] in (400, 422)

    def test_valid_pipeline_returns_200(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-executions", params={"name": name})
        assert resp["status"] == 200


class TestPipelineDAG:
    """GET /api/pipeline-dag?name=..."""

    def test_requires_name_param(self, api):
        resp = api.get("/api/pipeline-dag")
        assert resp["status"] in (400, 422)

    def test_valid_pipeline_returns_200(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-dag", params={"name": name})
        assert resp["status"] == 200


class TestPipelineLogs:
    """GET /api/pipeline-logs?name=..."""

    def test_requires_name_param(self, api):
        resp = api.get("/api/pipeline-logs")
        assert resp["status"] in (400, 422)

    def test_valid_pipeline_returns_200(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-logs", params={"name": name})
        assert resp["status"] == 200


class TestPipelineMetrics:
    """GET /api/pipeline-metrics?name=..."""

    def test_requires_name_param(self, api):
        resp = api.get("/api/pipeline-metrics")
        assert resp["status"] in (400, 422)

    def test_valid_pipeline_returns_200(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-metrics", params={"name": name})
        assert resp["status"] == 200


# =============================================================================
# Write operations (marked so they can be skipped)
# =============================================================================

@pytest.mark.write
class TestPipelineRun:
    """POST /api/pipeline-run — triggers a real execution."""

    def test_requires_name_param(self, api):
        resp = api.post("/api/pipeline-run")
        assert resp["status"] in (400, 422)

    def test_unknown_pipeline_fails(self, api):
        resp = api.post("/api/pipeline-run", params={"name": "__nonexistent_e2e_test__"})
        assert resp["status"] in (400, 404, 500)
