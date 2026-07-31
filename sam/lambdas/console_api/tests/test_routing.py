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
        ("GET", "/api/health/simple"),
        ("GET", "/api/metrics"),
    }
    assert r.get("GET", "/api/health") == (health.health_check, None)
    assert r.get("GET", "/api/health/simple") == (health.health_check_simple, None)
    assert r.get("GET", "/api/metrics") == (health.get_metrics, None)
