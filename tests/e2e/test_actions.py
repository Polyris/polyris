"""
E2E Tests — operator actions (ADR #137 §3, §4, §5): their answers to what they cannot act on.

None of these changes state: every request names a task or a run that does not exist. The happy paths, which
do, run in the coordination smoke (run_coordination_smoke.py).
"""

import pytest

GHOST_TASK = "e2e-no-such-task-2000-01-01--2000-01-01-00000000"
GHOST_RUN = "e2e-no-such-pipeline-run-2000-01-01-00000000"


class TestTaskActions:
    """POST /api/task-skip, -fail, -success, -stop, -restart"""

    @pytest.mark.parametrize("path", ["/api/task-skip", "/api/task-fail", "/api/task-success", "/api/task-stop",
                                      "/api/task-restart", "/api/task-retry"])
    def test_a_task_that_does_not_exist_is_404(self, api, path):
        resp = api.post(path, body={"date": "2000-01-01"}, params={"name": GHOST_TASK})
        assert resp["status"] == 404, resp

    def test_a_restart_with_dependants_of_a_task_that_does_not_exist_is_404(self, api):
        resp = api.post("/api/task-restart", body={"date": "2000-01-01", "with_dependants": True},
                        params={"name": GHOST_TASK})
        assert resp["status"] == 404, resp


class TestRunActions:
    """POST /api/execution-stop, -pause, -resume"""

    @pytest.mark.parametrize("path, key", [("/api/execution-stop", "pipeline_execution"),
                                           ("/api/execution-pause", "id"), ("/api/execution-resume", "id")])
    def test_a_run_without_its_run_row_is_409_and_names_why(self, api, path, key):
        resp = api.post(path, params={key: GHOST_RUN})
        assert resp["status"] == 409, resp
        assert "error" in resp["body"] and "pipeline_execution" in resp["body"]

    def test_a_stop_without_a_run_is_400(self, api):
        resp = api.post("/api/execution-stop", params={"pipeline_execution": ""})
        assert resp["status"] == 400, resp

    def test_the_pause_has_no_extend_route(self, api):
        resp = api.post("/api/execution-extend", params={"id": GHOST_RUN})
        assert resp["status"] in (403, 404), resp
