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


#: The pipeline-specs table that the deploy tests' fake platform stacks name (``PipelineSpecsTable``).
SPECS_TABLE = "acme-dev-polyris-pipeline-specs"
#: The registry tables those fake platform stacks name.
REGISTRY_TABLES = ("registry", "my-registry")


@pytest.fixture
def platform_dynamodb(monkeypatch):
    """A DynamoDB client on moto with the platform's pipeline-specs and registry tables.

    polyris-deploy writes the task specs and reads them back before any change set
    (ADR #137 §8); deploy tests hand this client to ``session.client("dynamodb")`` so
    that step runs for real, as the platform's table answers.
    """
    from moto import mock_aws
    import boto3

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName=SPECS_TABLE, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}])
        for name in REGISTRY_TABLES:
            client.create_table(
                TableName=name, BillingMode="PAY_PER_REQUEST",
                KeySchema=[{"AttributeName": "pipeline_name", "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": "pipeline_name", "AttributeType": "S"}])
        yield client
