"""Integration tests for backfill ID collision handling against real DDB
semantics (via moto).

Per CLAUDE.md #14 (mock at the external boundary), unit tests with
pytest-mock can verify "we called put_item with a ConditionExpression",
but they can't verify the conditional actually works the way DDB does.
These tests use moto to run real boto3 calls against an in-memory DDB
that behaves like real AWS — same conditional semantics, same
ClientError shapes, same field-type coercion.

If these pass, we know:
- `put_if_new` actually rejects duplicates (not just claims to)
- collision returns False, not raises
- non-collision DDB errors propagate correctly
- the retry loop in `start_backfill` actually retries on real
  ConditionalCheckFailedException, not on the mock's idea of one

This is the "smoke at the integration boundary" gate per CLAUDE.md #13
that unit tests with stub mocks cannot provide.
"""

import os
import sys

import boto3
import pytest

# Add sam/lambdas/console_api to path so we can import the production code
SAM_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'sam', 'lambdas', 'console_api',
)
if SAM_PATH not in sys.path:
    sys.path.insert(0, SAM_PATH)

try:
    from moto import mock_aws
except ImportError:
    pytest.skip("moto not installed — skipping integration tests", allow_module_level=True)


TABLE_NAME = 'test-pipeline-tokens'


@pytest.fixture
def ddb_table(monkeypatch):
    """Spin up a fake DDB table that matches production schema.

    Schema mirrors the real PipelineTokensTable in sam/template.yaml:
        HASH: execution_name (S)
        GSIs: backfill-id-index (backfill_id, HASH)
              date-pipeline-index (date HASH, pipeline_name RANGE)
    """
    # moto needs creds + region to satisfy boto3 client config
    monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'test')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'test')
    # config.TABLE_NAME reads from DYNAMODB_TABLE env var
    monkeypatch.setenv('DYNAMODB_TABLE', TABLE_NAME)

    with mock_aws():
        ddb = boto3.resource('dynamodb', region_name='us-east-1')
        ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
            AttributeDefinitions=[
                {'AttributeName': 'execution_name', 'AttributeType': 'S'},
                {'AttributeName': 'backfill_id', 'AttributeType': 'S'},
                {'AttributeName': 'date', 'AttributeType': 'S'},
                {'AttributeName': 'pipeline_name', 'AttributeType': 'S'},
            ],
            GlobalSecondaryIndexes=[
                {
                    'IndexName': 'backfill-id-index',
                    'KeySchema': [{'AttributeName': 'backfill_id', 'KeyType': 'HASH'}],
                    'Projection': {'ProjectionType': 'ALL'},
                    'ProvisionedThroughput': {'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5},
                },
                {
                    'IndexName': 'date-pipeline-index',
                    'KeySchema': [
                        {'AttributeName': 'date', 'KeyType': 'HASH'},
                        {'AttributeName': 'pipeline_name', 'KeyType': 'RANGE'},
                    ],
                    'Projection': {'ProjectionType': 'ALL'},
                    'ProvisionedThroughput': {'ReadCapacityUnits': 5, 'WriteCapacityUnits': 5},
                },
            ],
            BillingMode='PAY_PER_REQUEST',
        )
        table = ddb.Table(TABLE_NAME)
        yield table


@pytest.fixture
def repo(ddb_table, monkeypatch):
    """A real BackfillsRepo wired to the moto table.

    config.dynamodb is created at module-import time — must re-import dal
    modules inside the moto context so they bind to the fake resource.
    """
    # Force re-import of config + dal so boto3 binds to moto's fake AWS
    for mod_name in list(sys.modules):
        if mod_name == 'config' or mod_name.startswith('dal.'):
            del sys.modules[mod_name]
    from dal.backfills_repo import BackfillsRepo
    return BackfillsRepo()


# ──────────────────────────────────────────────────────────────────────────────
# put_if_new — conditional semantics against real DDB behavior
# ──────────────────────────────────────────────────────────────────────────────


class TestPutIfNewIntegration:
    """The unit tests verify that put_if_new sets the right ConditionExpression.
    These verify the conditional actually works against DDB."""

    def test_first_put_succeeds(self, repo):
        result = repo.put_if_new({
            'execution_name': 'bf-aaaaaaaa',
            'status': 'pending',
        })
        assert result is True
        # Verify it's actually in the table
        got = repo.get('bf-aaaaaaaa')
        assert got is not None
        assert got['status'] == 'pending'

    def test_duplicate_put_returns_false_not_raise(self, repo):
        """The bug we're guarding against: if DDB raised on duplicate, the
        handler's loop would crash on first attempt instead of retrying."""
        # First put — succeeds
        assert repo.put_if_new({
            'execution_name': 'bf-dup',
            'status': 'pending',
        }) is True
        # Second put with same ID — should return False, not raise
        result = repo.put_if_new({
            'execution_name': 'bf-dup',
            'status': 'pending',
        })
        assert result is False
        # And the original record should be untouched
        got = repo.get('bf-dup')
        assert got['status'] == 'pending'

    def test_different_ids_dont_collide(self, repo):
        """Sanity — the conditional is keyed on execution_name, nothing else."""
        for i in range(10):
            assert repo.put_if_new({
                'execution_name': f'bf-{i:08x}',
                'status': 'pending',
            }) is True

    def test_collision_does_not_modify_existing(self, repo):
        """A failed conditional put must leave the existing row untouched —
        otherwise retry-with-new-id could clobber state."""
        repo.put_if_new({
            'execution_name': 'bf-stable',
            'status': 'completed',  # terminal status
            'completed_partitions': 42,
        })
        # Attempt to overwrite with different content
        result = repo.put_if_new({
            'execution_name': 'bf-stable',
            'status': 'pending',
            'completed_partitions': 0,
        })
        assert result is False
        # Original row unchanged
        got = repo.get('bf-stable')
        assert got['status'] == 'completed'
        assert int(got['completed_partitions']) == 42


# ──────────────────────────────────────────────────────────────────────────────
# Collision retry loop (start_backfill) against real DDB
