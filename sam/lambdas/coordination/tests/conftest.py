"""Fixtures for the coordination Lambda tests.

DynamoDB is moto, with tables built from sam/template.yaml, so key schemas,
indexes and projections are the deployed ones (Principle #13). Step
Functions is a fake client at the boto3 boundary (Principle #14).
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordination_testkit import NOW, TABLES, FakeSfn, template  # noqa: E402 — the paths above come first

for _var, (_logical, _name) in TABLES.items():
    os.environ[_var] = _name   # read by env.py at import, so before any test module imports it
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def ddb():
    from moto import mock_aws
    import boto3

    resources = template()["Resources"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        for logical, name in TABLES.values():
            props = resources[logical]["Properties"]
            kwargs: Dict[str, Any] = {
                "TableName": name,
                "KeySchema": props["KeySchema"],
                "AttributeDefinitions": props["AttributeDefinitions"],
                "BillingMode": "PAY_PER_REQUEST",
            }
            if props.get("GlobalSecondaryIndexes"):
                kwargs["GlobalSecondaryIndexes"] = [
                    {k: gsi[k] for k in ("IndexName", "KeySchema", "Projection")}
                    for gsi in props["GlobalSecondaryIndexes"]
                ]
            if props.get("StreamSpecification"):
                kwargs["StreamSpecification"] = {"StreamEnabled": True, **props["StreamSpecification"]}
            resource.create_table(**kwargs)
        yield resource


@pytest.fixture
def sfn() -> FakeSfn:
    return FakeSfn()


@pytest.fixture
def reconciler(ddb, sfn):
    from reconcile import Reconciler

    return Reconciler(ddb, sfn, clock=lambda: NOW)


@pytest.fixture
def tokens(ddb):
    return ddb.Table(TABLES["TOKENS_TABLE"][1])


@pytest.fixture
def subscriptions(ddb):
    return ddb.Table(TABLES["SUBSCRIPTIONS_TABLE"][1])


@pytest.fixture
def asset_events(ddb):
    return ddb.Table(TABLES["ASSET_EVENTS_TABLE"][1])


@pytest.fixture
def registry(ddb):
    return ddb.Table(TABLES["PIPELINES_TABLE"][1])
