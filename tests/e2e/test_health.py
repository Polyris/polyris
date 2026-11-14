"""
E2E Tests — Health & Metrics endpoints.

These are the safest E2E tests: read-only, no side effects.
Good for smoke-testing a deployment.
"""

import pytest

#: What GET /api/health shows a caller without a valid token: each check's status word, nothing else.
MINIMAL_KEYS = {"status", "timestamp", "response_time_ms", "detail", "checks"}
WORDS = {"healthy", "degraded", "unhealthy"}


@pytest.fixture(scope="module")
def anonymous(api):
    """The same API, called without a token."""
    return type(api)(api.base_url, "")


def _auth_enforced(anonymous) -> bool:
    """Whether the deployment checks tokens: a protected route answers 401 without one."""
    return anonymous.get("/api/pipelines")["status"] == 401


def _header(resp, name: str) -> str:
    return next((value for key, value in resp["headers"].items() if key.lower() == name.lower()), "")


# =============================================================================
# Health Endpoints
# =============================================================================

class TestHealthLive:
    """GET /api/health/live — liveness probe, no downstream call."""

    def test_returns_200(self, api):
        resp = api.get("/api/health/live")
        assert resp["status"] == 200

    def test_response_has_status_ok(self, api):
        resp = api.get("/api/health/live")
        assert resp["body"]["status"] == "ok"

    def test_response_has_timestamp(self, api):
        resp = api.get("/api/health/live")
        assert "timestamp" in resp["body"]

    def test_the_old_path_is_gone(self, api):
        """/api/health/simple was renamed without an alias."""
        assert api.get("/api/health/simple")["status"] == 404


class TestHealthDetailed:
    """GET /api/health — comprehensive health check."""

    def test_returns_200_or_503(self, api):
        resp = api.get("/api/health")
        assert resp["status"] in (200, 503)

    def test_response_has_checks(self, api):
        resp = api.get("/api/health")
        body = resp["body"]
        assert "status" in body
        assert body["status"] in ("healthy", "degraded", "unhealthy")
        assert "checks" in body

    def test_dynamodb_check_present(self, api):
        resp = api.get("/api/health")
        checks = resp["body"]["checks"]
        assert "dynamodb" in checks
        assert "status" in checks["dynamodb"]

    def test_stepfunctions_check_present(self, api):
        resp = api.get("/api/health")
        checks = resp["body"]["checks"]
        assert "stepfunctions" in checks
        assert "status" in checks["stepfunctions"]

    def test_response_time_reported(self, api):
        resp = api.get("/api/health")
        assert "response_time_ms" in resp["body"]
        assert resp["body"]["response_time_ms"] >= 0


class TestHealthAccess:
    """GET /api/health shows its details only to a caller with a valid token, or to anyone while auth is off."""

    def test_without_a_token_only_the_status_words(self, anonymous):
        resp = anonymous.get("/api/health")
        body = resp["body"]
        assert resp["status"] in (200, 503)
        assert (resp["status"] == 503) == (body["status"] == "unhealthy")
        assert "no-store" in _header(resp, "Cache-Control")
        if not _auth_enforced(anonymous):
            assert body["detail"] == "full"                 # auth is off: anyone sees the details
            return
        assert set(body) == MINIMAL_KEYS
        assert body["detail"] == "minimal"
        for name, check in body["checks"].items():
            assert set(check) == {"status"} and check["status"] in WORDS, name

    def test_with_a_token_the_details_and_the_findings(self, api):
        if not api.token:
            pytest.skip("POLYRIS_ID_TOKEN not set")
        resp = api.get("/api/health")
        body = resp["body"]
        assert resp["status"] in (200, 503)
        assert (resp["status"] == 503) == (body["status"] == "unhealthy")
        assert "no-store" in _header(resp, "Cache-Control")
        assert body["detail"] == "full"
        coordination = body["checks"]["coordination"]
        assert set(coordination) == {"status", "report"} and coordination["status"] in WORDS
        assert isinstance(body["findings"], list)
        for finding in body["findings"]:
            assert isinstance(finding.get("title"), str) and finding["title"], finding
        if coordination["status"] == "unhealthy":
            assert body["findings"], "an unhealthy engine names what is wrong"


class TestMetrics:
    """GET /api/metrics — system metrics."""

    def test_returns_200(self, api):
        resp = api.get("/api/metrics")
        assert resp["status"] == 200

    def test_response_has_metrics(self, api):
        resp = api.get("/api/metrics")
        body = resp["body"]
        assert "metrics" in body
        assert "date" in body
        assert "timestamp" in body

    def test_tasks_metrics_present(self, api):
        resp = api.get("/api/metrics")
        metrics = resp["body"]["metrics"]
        assert "tasks" in metrics

    def test_pipelines_metrics_present(self, api):
        resp = api.get("/api/metrics")
        metrics = resp["body"]["metrics"]
        assert "pipelines" in metrics
