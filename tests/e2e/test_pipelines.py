"""
E2E Tests — Pipeline endpoints.

Split into read-only tests (safe to run anytime) and write tests (trigger executions).
Write tests are marked with @pytest.mark.write so you can skip them: pytest -m "not write"
"""
import pytest

from polyris.constants import PipelineStatus, TaskStatus

#: Every value a pipeline's `status` may take (ADR #112): ExecutionStatus plus idle.
PIPELINE_STATUSES = {v for k, v in vars(PipelineStatus).items() if not k.startswith("_")}
#: What `waiting_on` may say: the latest run waits for an operator, or nothing (ADR #136).
WAITING_ON = {None, TaskStatus.WAITING_DECISION.value, TaskStatus.WAITING_PAUSED.value}


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


class TestPipelinesSidebarContract:
    """GET /api/pipelines?stats=true — the answer the console's sidebar reads (ADR #136).

    A registry that cannot be read answers 500; that path is pinned by the console's unit
    tests (test_pipelines_list_reconcile.py), since a live stack cannot be made to fail it.
    """

    def test_answers_the_pipelines_and_whether_their_stats_are_complete(self, api):
        resp = api.get("/api/pipelines", params={"stats": "true"})
        assert resp["status"] == 200
        body = resp["body"]
        assert isinstance(body, dict), f"expected an object, got {type(body).__name__}"
        assert isinstance(body.get("stats_incomplete"), bool)
        assert isinstance(body.get("pipelines"), list)

    def test_each_pipeline_carries_its_latest_status_runs_and_wait(self, api):
        pipelines = api.get("/api/pipelines", params={"stats": "true"})["body"]["pipelines"]
        if not pipelines:
            pytest.skip("No pipelines registered")
        for p in pipelines:
            name = p.get("name")
            assert isinstance(name, str) and name
            assert p.get("status") in PIPELINE_STATUSES, f"{name}: status {p.get('status')!r}"
            assert "waiting_on" in p and p["waiting_on"] in WAITING_ON, f"{name}: waiting_on {p.get('waiting_on')!r}"
            assert "today_stats" not in p and "progress" not in p, f"{name}: removed fields are back"
            runs = p.get("recent_runs")
            if runs is None:
                assert p["status"] == PipelineStatus.IDLE, f"{name}: no runs but status {p['status']!r}"
                continue
            assert isinstance(runs, list) and len(runs) <= 10, f"{name}: recent_runs {runs!r}"
            for run in runs:
                assert {"date", "exec", "status"} <= set(run), f"{name}: run {run!r}"
                assert run["status"] in PIPELINE_STATUSES - {PipelineStatus.IDLE}, f"{name}: run {run!r}"
            if runs:
                assert p["status"] == runs[0]["status"], f"{name}: status is not the latest run's"


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
