"""
SDK Tests - conftest.py

Tests for the polyris Python SDK (DAG definitions, ASL generation, templates).
These tests validate the library layer and can run independently of infrastructure.

Path setup: adds repo root to sys.path so `from polyris import ...` works.
"""
import os
import sys

# Repo root is two levels up from tests/sdk/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


import pytest  # noqa: E402

# Environment variables that change which AWS account, region, profile or
# polyris stage code resolves. A maintainer's shell often has them set (e.g.
# `export AWS_PROFILE=… AWS_REGION=…` left over from a smoke test); tests must
# not see them, or the result depends on whoever runs the suite. Hit on the
# v1.1.4 release: `make check` failed with 9 tests reading AWS_PROFILE.
_AMBIENT_ENV = (
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "POLYRIS_PROFILE", "POLYRIS_REGION", "POLYRIS_STAGE", "POLYRIS_NAMESPACE",
    "POLYRIS_STACK", "POLYRIS_STACK_NAME",
)


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path_factory):
    """Every SDK test starts without the developer's AWS / polyris settings.

    Tests that need a value set it themselves with monkeypatch. The AWS config
    and credentials files point at an empty location, so the real ~/.aws can't
    leak a profile or region either.
    """
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    empty = tmp_path_factory.getbasetemp() / "no-aws-config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(empty / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(empty / "credentials"))
