"""Fixtures for the coordination Lambda tests.

DynamoDB is moto, with tables built from sam/template.yaml, so key schemas,
indexes and projections are the deployed ones (Principle #13). Step
Functions is a fake client at the boto3 boundary (Principle #14).
"""

import os
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from coordination_testkit import NOW, TABLES, FakeSfn, create_tables  # noqa: E402 — the paths above come first

for _var, (_logical, _name) in TABLES.items():
    os.environ[_var] = _name   # read by env.py at import, so before any test module imports it
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


@pytest.fixture
def ddb():
    from moto import mock_aws
    import boto3

    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        create_tables(resource, dict(TABLES.values()))
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
def consumers(ddb):
    return ddb.Table(TABLES["ASSET_SUBSCRIPTIONS_TABLE"][1])


@pytest.fixture
def registry(ddb):
    return ddb.Table(TABLES["PIPELINES_TABLE"][1])


@pytest.fixture
def specs(ddb):
    return ddb.Table(TABLES["SPECS_TABLE"][1])


@pytest.fixture
def rows(ddb, sfn, monkeypatch):
    """1.3 runs as Start_Run writes them, and the dispatcher over them (ADR #137), on the testkit's clock."""
    from datetime import datetime
    from types import SimpleNamespace

    import boto3

    import env
    import start_run
    from coordination_testkit import DATE, RUN, STARTED_AT
    from dispatcher import Dispatcher
    from polyris.coordination import spec_store, task_spec

    client = boto3.client("dynamodb", region_name="us-east-1")
    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_dynamodb_client", client)

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):                               # type: ignore[override]
            return NOW

    monkeypatch.setattr(start_run, "datetime", Frozen)

    def start(specs, *, execution: str = RUN, pipeline: str = "orders", **inputs):
        version = task_spec.build_version(pipeline, specs)
        spec_store.write(client, TABLES["SPECS_TABLE"][1], version, sleep=lambda _: None)
        start_run.handler({"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id,
                           "task_count": version.task_count, "pipeline_name": pipeline, "execution_name": execution,
                           "execution_start": STARTED_AT, "token": f"{execution}-token", "retry_count": 0,
                           "redrive_count": 0, "date": DATE, "input": inputs}, None)
        return version

    dispatcher = Dispatcher(client, sfn, resource=ddb, tokens_table=TABLES["TOKENS_TABLE"][1],
                            subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1], specs_table=TABLES["SPECS_TABLE"][1],
                            asset_events_table=TABLES["ASSET_EVENTS_TABLE"][1],
                            pipelines_table=TABLES["PIPELINES_TABLE"][1], run_task_arn="arn:run-task",
                            clock=lambda: NOW, sleep=lambda _: None)
    return SimpleNamespace(client=client, start=start, dispatcher=dispatcher)
