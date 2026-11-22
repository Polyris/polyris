"""
Backend Tests - conftest.py

Tests for the backend API layer (Lambda handlers, routes, alerting).
These tests validate the deployed service logic and can run independently.

Path setup: adds console_api lambda dir and repo root to sys.path.
"""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONSOLE_API = os.path.join(REPO_ROOT, 'sam', 'lambdas', 'console_api')

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if CONSOLE_API not in sys.path:
    sys.path.insert(0, CONSOLE_API)


import pytest  # noqa: E402

# No backend test may reach a real AWS account: a run of test_api_routes.py once sent a real DynamoDB Query with
# the developer's default profile (TR-6d; AWS denied it). The developer's AWS settings are cleared and fake
# credentials set before any test module imports code that builds a client, and again around every test.
_AMBIENT_ENV = (
    "AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION",
    "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
)
_FAKE_AWS = {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_DEFAULT_REGION": "us-east-1",
             "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull}
for _name in _AMBIENT_ENV:
    os.environ.pop(_name, None)
os.environ.update(_FAKE_AWS)


@pytest.fixture(autouse=True)
def _hermetic_aws(monkeypatch):
    """Every backend test runs on fake credentials and without the developer's AWS settings."""
    for name in _AMBIENT_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in _FAKE_AWS.items():
        monkeypatch.setenv(name, value)


SPECS_TABLE = "test-pipeline-specs"


@pytest.fixture
def deploy_specs(mocker, monkeypatch):
    """Write a pipeline's task specs to a moto ``pipeline-specs`` table, as polyris-deploy does (ADR #137 §8).

    Returns ``deploy(pipeline, specs)``, which gives the registry entry that names the version;
    each spec needs a ``task_id``, and the keys every spec has default to a plain sfn task.
    """
    import importlib

    import boto3
    from moto import mock_aws
    from polyris.coordination import spec_store, task_spec

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=SPECS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": task_spec.PK, "KeyType": "HASH"},
                       {"AttributeName": task_spec.SK, "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": task_spec.PK, "AttributeType": "S"},
                                  {"AttributeName": task_spec.SK, "AttributeType": "S"}])
        config = importlib.import_module("config")
        mocker.patch.object(config, "_dynamodb_client", client)
        repo = importlib.import_module("dal.specs_repo").specs_repo
        mocker.patch.object(repo, "_table_name", SPECS_TABLE)
        mocker.patch.object(repo, "_versions", type(repo._versions)())

        def deploy(pipeline, specs):
            full = [{"task_type": "sfn", "dependencies": [], "trigger_rule": "all_success", **s} for s in specs]
            version = task_spec.build_version(pipeline, full)
            spec_store.write(client, SPECS_TABLE, version, sleep=lambda _: None)
            return {"pipeline_name": pipeline, "spec_id": version.spec_id, "task_count": version.task_count}

        yield deploy
