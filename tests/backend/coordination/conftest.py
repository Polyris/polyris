"""Coordination harness (ADR #130, Testing): see README.md in this directory."""

import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
COORDINATION = REPO_ROOT / "sam" / "lambdas" / "coordination"

for path in (HERE, COORDINATION / "tests", COORDINATION):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

from stages import BOUND_STAGES, STAGES  # noqa: E402 — needs the paths above


def pytest_configure(config):
    config.addinivalue_line("markers", "needs_binding(stage): the scenario's code path arrives with `stage` (ADR #130)")
    config.addinivalue_line("markers", "defect(id): the ADR #130 defect this scenario pins (defects.py)")


def pytest_collection_modifyitems(config, items):
    for item in items:
        for mark in item.iter_markers("needs_binding"):
            stage = mark.args[0]
            if stage not in STAGES:
                raise pytest.UsageError(f"{item.nodeid}: unknown stage {stage!r}; expected one of {STAGES}")
            if stage not in BOUND_STAGES:
                item.add_marker(pytest.mark.skip(reason=f"needs the {stage} stage of ADR #130"))


@pytest.fixture
def world():
    import boto3
    from moto import mock_aws

    from world import World

    with mock_aws():
        built = World(boto3.client("dynamodb", region_name="us-east-1"))
        try:
            yield built
        finally:
            built.close()
