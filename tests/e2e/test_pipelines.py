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
#: The statuses a run's summary gives (ADR #112, ADR #137 §4).
RUN_STATUSES = {"running", "success", "failed", "aborted"}


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
        assert isinstance(body.get("index_building"), bool)          # run-start-index still built (ADR #137 §5)
        assert not body["index_building"] or body["stats_incomplete"]
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
                assert {"date", "started_at", "exec", "status", "failed_tasks"} <= set(run), f"{name}: run {run!r}"
                # From the run row's summary; no console route asks Step Functions (ADR #137 §4, §5).
                assert run["status"] in RUN_STATUSES, f"{name}: run {run!r}"
            if runs:
                assert p["status"] == runs[0]["status"], f"{name}: status is not the latest run's"
                assert p["failed_tasks"] == runs[0]["failed_tasks"], f"{name}: failed_tasks is not the latest run's"
            ordered = [(r["date"], r["started_at"]) for r in runs]
            assert ordered == sorted(ordered, reverse=True), f"{name}: recent_runs not newest first: {ordered}"


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
        assert "selected_execution" in resp["body"] and isinstance(resp["body"]["tasks"], list)

    def test_the_latest_run_of_its_date_is_the_one_shown(self, api, registered_pipelines):
        """ADR #137 §5: found with a query of pipeline-date-index, no scan; the run named is the date's latest."""
        name, run = _latest_run(api, registered_pipelines)
        body = api.get("/api/pipeline-status", params={"name": name, "date": run["date"]})["body"]
        assert body["selected_execution"] is not None and body["pipeline_name"] == name


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
        if resp["status"] == 503 and resp["body"].get("code") == "INDEX_BUILDING":
            pytest.skip("run-start-index is still being built")
        assert resp["status"] == 200
        assert "next" in resp["body"]

    def test_each_run_says_its_status_and_failed_tasks_newest_first(self, api, registered_pipelines):
        """ADR #137 §4, §5: run rows, newest first; the status from the run row's summary."""
        name, _ = _latest_run(api, registered_pipelines)
        runs = api.get("/api/pipeline-executions", params={"name": name})["body"]["executions"]
        for run in runs:
            assert run["status"] in RUN_STATUSES and isinstance(run["failed_tasks"], int), run
        starts = [r["started_at"] for r in runs]
        assert starts == sorted(starts, reverse=True)

    def test_a_range_and_a_date_find_the_run(self, api, registered_pipelines):
        """D100: a range walks every start day of the window with a filter on the logical date."""
        name, run = _latest_run(api, registered_pipelines)
        ranged = api.get("/api/pipeline-executions",
                         params={"name": name, "start_date": run["date"], "end_date": run["date"]})["body"]
        dated = api.get("/api/pipeline-executions", params={"name": name, "date": run["date"]})["body"]
        assert run["execution_id"] in [e["execution_id"] for e in ranged["executions"]]
        assert run["execution_id"] in [e["execution_id"] for e in dated["executions"]]

    def test_a_bad_range_is_refused(self, api, registered_pipelines):
        if not registered_pipelines:
            pytest.skip("No pipelines registered")
        name = registered_pipelines[0] if isinstance(registered_pipelines[0], str) else registered_pipelines[0]["name"]
        resp = api.get("/api/pipeline-executions", params={"name": name, "start_date": "2026-13-01"})
        assert resp["status"] == 400


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
        assert resp["body"]["dag_source"] in {"registry", "inferred"}

    def test_a_run_started_on_1_3_draws_its_own_spec_version(self, api, registered_pipelines):
        """ADR #137 §8: the graph the run ran with, from the spec version its run row names."""
        name, run = _latest_run(api, registered_pipelines)
        body = api.get("/api/pipeline-dag", params={"name": name, "pipeline_execution": run["execution_id"]})["body"]
        assert body["dag_source"] == "run" and body["nodes"]


def _latest_run(api, registered_pipelines):
    """A pipeline with a run started on 1.3, and that run; skips when no pipeline has one."""
    for p in registered_pipelines:
        name = p if isinstance(p, str) else p["name"]
        resp = api.get("/api/pipeline-executions", params={"name": name, "page_size": "1"})
        if resp["status"] == 503 and resp["body"].get("code") == "INDEX_BUILDING":
            pytest.skip("run-start-index is still being built")
        runs = resp["body"].get("executions") or []
        if runs:
            return name, runs[0]
    pytest.skip("No pipeline has a run started on 1.3")


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
