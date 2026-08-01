"""
E2E Tests — Health & Metrics endpoints.

These are the safest E2E tests: read-only, no side effects.
Good for smoke-testing a deployment.
"""


# =============================================================================
# Health Endpoints
# =============================================================================

class TestHealthSimple:
    """GET /api/health/simple — lightweight probe."""

    def test_returns_200(self, api):
        resp = api.get("/api/health/simple")
        assert resp["status"] == 200

    def test_response_has_status_ok(self, api):
        resp = api.get("/api/health/simple")
        assert resp["body"]["status"] == "ok"

    def test_response_has_timestamp(self, api):
        resp = api.get("/api/health/simple")
        assert "timestamp" in resp["body"]


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
