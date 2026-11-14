"""Tests for the plugin route registry (ADR #97).

Covers the Router mechanism and the reference migration of the health module.
The full route table (router-built) is asserted unchanged by
tests/sdk/test_templates.py::test_route_table_completeness (58 routes with ee present).
"""
import pytest

from routing import Router
from routes import health


def _h(event):
    return {}


def test_router_add_and_get():
    r = Router()
    r.add("GET", "/x", _h)
    r.add("POST", "/y", _h, "id")
    assert r.get("GET", "/x") == (_h, None)
    assert r.get("POST", "/y") == (_h, "id")
    assert r.get("GET", "/missing") is None


def test_router_rejects_duplicate_route():
    r = Router()
    r.add("GET", "/x", _h)
    with pytest.raises(ValueError):
        r.add("GET", "/x", _h)


def test_health_module_registers_its_routes():
    """health.register() reproduces exactly the health/metrics routes."""
    r = Router()
    health.register(r)
    assert set(r.table.keys()) == {
        ("GET", "/api/health"),
        ("GET", "/api/health/live"),
        ("GET", "/api/metrics"),
    }
    assert r.get("GET", "/api/health") == (health.health_check, None)
    assert r.get("GET", "/api/health/live") == (health.health_check_live, None)
    assert r.get("GET", "/api/metrics") == (health.get_metrics, None)


def test_the_old_liveness_path_is_no_longer_routed():
    """/api/health/simple became /api/health/live, without an alias."""
    import main

    assert ("GET", "/api/health/simple") not in main.ROUTES
    event = {"rawPath": "/api/health/simple", "requestContext": {"http": {"method": "GET"}, "stage": "$default"}}
    assert main.handler(event, None)["statusCode"] == 404
