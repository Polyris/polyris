"""
Polyris E2E Tests — conftest.py

End-to-end tests that hit the real deployed API.
Requires:
  - POLYRIS_API_URL  (e.g. https://abc123.execute-api.us-east-1.amazonaws.com)
  - POLYRIS_ID_TOKEN (Cognito ID token, optional if auth is disabled)

Usage:
  POLYRIS_API_URL=https://... pytest tests/e2e/ -v
  POLYRIS_API_URL=https://... POLYRIS_ID_TOKEN=ey... pytest tests/e2e/ -v
"""
import os
import sys
import time
import pytest
import urllib.request
import urllib.error
import json
from typing import Optional, Dict, Any

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


# =============================================================================
# Configuration
# =============================================================================

API_URL = os.environ.get("POLYRIS_API_URL", "").rstrip("/")
ID_TOKEN = os.environ.get("POLYRIS_ID_TOKEN", "")

# Timeouts
REQUEST_TIMEOUT = 30  # seconds per request
PIPELINE_RUN_TIMEOUT = 120  # seconds to wait for pipeline execution


def _skip_if_no_api():
    if not API_URL:
        pytest.skip("POLYRIS_API_URL not set — skipping E2E tests")


# =============================================================================
# HTTP Client (stdlib only — no extra dependencies)
# =============================================================================

class APIClient:
    """Lightweight HTTP client for E2E tests. Uses only stdlib."""

    def __init__(self, base_url: str, token: str = ""):
        self.base_url = base_url
        self.token = token

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        return self._execute(req)

    def post(self, path: str, body: Optional[Dict] = None, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
        return self._execute(req)

    def put(self, path: str, body: Optional[Dict] = None, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
        data = json.dumps(body or {}).encode()
        req = urllib.request.Request(url, data=data, headers=self._headers(), method="PUT")
        return self._execute(req)

    def delete(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"
        req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
        return self._execute(req)

    def _execute(self, req: urllib.request.Request) -> Dict[str, Any]:
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                body = resp.read().decode()
                return {
                    "status": resp.status,
                    "body": json.loads(body) if body else {},
                    "headers": dict(resp.headers),
                }
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            return {
                "status": e.code,
                "body": json.loads(body) if body else {},
                "headers": dict(e.headers) if e.headers else {},
                "error": str(e),
            }
        except urllib.error.URLError as e:
            pytest.fail(f"Connection failed: {e.reason}")


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture(scope="session")
def api() -> APIClient:
    """Shared API client for all E2E tests."""
    _skip_if_no_api()
    return APIClient(API_URL, ID_TOKEN)


@pytest.fixture(scope="session")
def registered_pipelines(api: APIClient) -> list:
    """Get list of registered pipelines (cached for session)."""
    resp = api.get("/api/pipelines")
    assert resp["status"] == 200, f"Failed to list pipelines: {resp}"
    return resp["body"] if isinstance(resp["body"], list) else resp["body"].get("pipelines", [])


# =============================================================================
# Helpers
# =============================================================================

def wait_for_execution(api: APIClient, pipeline_name: str, execution_id: str,
                       timeout: int = PIPELINE_RUN_TIMEOUT, poll_interval: int = 5) -> Dict:
    """Poll until execution reaches a terminal state."""
    terminal = {"success", "failed", "aborted", "skipped"}
    deadline = time.time() + timeout

    while time.time() < deadline:
        resp = api.get("/api/pipeline-executions", params={"name": pipeline_name})
        if resp["status"] == 200:
            executions = resp["body"] if isinstance(resp["body"], list) else resp["body"].get("executions", [])
            for ex in executions:
                if ex.get("execution_id") == execution_id and ex.get("status") in terminal:
                    return ex
        time.sleep(poll_interval)

    pytest.fail(f"Execution {execution_id} did not complete within {timeout}s")
