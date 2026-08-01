"""
Unit tests for dal.backfills_repo.BackfillsRepo.

Covers single-item ops (get/put/delete/update_status/increment_counter),
query ops (list_recent/list_active/query_child_executions), and the
sentinel/record_type defaults applied at put time.

Pattern follows test_assets_repo.py — mock the .table property to short-
circuit the dynamodb.Table(...) lookup. No moto, no AWS calls.
"""

import pytest


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_repo_with_table(mocker, table_mock):
    """Build a BackfillsRepo whose .table property returns our mock."""
    from dal.backfills_repo import BackfillsRepo
    repo = BackfillsRepo()
    mocker.patch.object(
        BackfillsRepo, 'table',
        new_callable=mocker.PropertyMock,
        return_value=table_mock,
    )
    return repo


# ──────────────────────────────────────────────────────────────────────────────
# Single-item ops
# ──────────────────────────────────────────────────────────────────────────────

class TestGet:
    def test_returns_backfill_record(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {
                'execution_name': 'bf-abc123',
                'record_type': 'backfill',
                'pipeline_name': '_polyris_bulk_backfill',
                'status': 'running',
            }
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.get('bf-abc123')
        assert result is not None
        assert result['execution_name'] == 'bf-abc123'

    def test_returns_none_if_missing(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {}  # no Item key
        repo = _make_repo_with_table(mocker, table)
        assert repo.get('bf-nonexistent') is None

    def test_returns_none_if_not_backfill_record(self, mocker):
        """Defensive: if execution_name happens to match but record_type is
        not 'backfill', treat as None to avoid leaking unrelated rows."""
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {
                'execution_name': 'bf-collides',
                'record_type': 'execution',  # not a Backfill
            }
        }
        repo = _make_repo_with_table(mocker, table)
        assert repo.get('bf-collides') is None

    def test_consistent_read_flag_passed(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {'execution_name': 'bf-abc', 'record_type': 'backfill'}
        }
        repo = _make_repo_with_table(mocker, table)
        repo.get('bf-abc', consistent=True)
        kwargs = table.get_item.call_args.kwargs
        assert kwargs.get('ConsistentRead') is True


class TestPut:
    def test_basic_put(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.put({
            'execution_name': 'bf-abc',
            'record_type': 'backfill',
            'pipeline_name': '_polyris_bulk_backfill',
            'status': 'pending',
        })
        table.put_item.assert_called_once()

    def test_put_sets_default_sentinel(self, mocker):
        """If caller forgets to set pipeline_name, the sentinel is applied."""
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.put({'execution_name': 'bf-abc', 'status': 'pending'})
        item = table.put_item.call_args.kwargs['Item']
        assert item['pipeline_name'] == '_polyris_bulk_backfill'
        assert item['record_type'] == 'backfill'

    def test_put_sets_default_ttl(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.put({'execution_name': 'bf-abc'})
        item = table.put_item.call_args.kwargs['Item']
        assert 'ttl' in item
        assert isinstance(item['ttl'], int)
        assert item['ttl'] > 0

    def test_put_preserves_caller_provided_fields(self, mocker):
        """Defaults don't override caller-supplied values."""
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        custom_ttl = 999999
        repo.put({
            'execution_name': 'bf-abc',
            'pipeline_name': '_polyris_bulk_backfill',
            'record_type': 'backfill',
            'ttl': custom_ttl,
            'status': 'running',
        })
        item = table.put_item.call_args.kwargs['Item']
        assert item['ttl'] == custom_ttl


class TestPutIfNew:
    """Conditional put — handles bf-id collisions safely. Required because
    bf-{8 hex} has ~10% collision probability at 30k records."""

    def test_returns_true_on_success(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        result = repo.put_if_new({'execution_name': 'bf-abc'})
        assert result is True
        call = table.put_item.call_args
        assert call.kwargs['ConditionExpression'] == 'attribute_not_exists(execution_name)'

    def test_returns_false_on_collision(self, mocker):
        """ConditionalCheckFailedException → False, not exception."""
        from botocore.exceptions import ClientError
        table = mocker.MagicMock()
        table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ConditionalCheckFailedException'}}, 'PutItem',
        )
        repo = _make_repo_with_table(mocker, table)
        result = repo.put_if_new({'execution_name': 'bf-abc'})
        assert result is False

    def test_raises_on_other_client_error(self, mocker):
        """Non-conditional errors (throttling, IAM, table-missing) must propagate
        so the caller can surface 5xx, not silently retry forever."""
        from botocore.exceptions import ClientError
        table = mocker.MagicMock()
        table.put_item.side_effect = ClientError(
            {'Error': {'Code': 'ProvisionedThroughputExceededException'}},
            'PutItem',
        )
        repo = _make_repo_with_table(mocker, table)
        with pytest.raises(ClientError):
            repo.put_if_new({'execution_name': 'bf-abc'})

    def test_applies_same_defaults_as_put(self, mocker):
        """put_if_new must apply sentinel, record_type, ttl just like put."""
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.put_if_new({'execution_name': 'bf-abc'})
        item = table.put_item.call_args.kwargs['Item']
        assert item['pipeline_name'] == '_polyris_bulk_backfill'
        assert item['record_type'] == 'backfill'
        assert 'ttl' in item


class TestUpdateStatus:
    def test_basic_status_update(self, mocker):
        table = mocker.MagicMock()
        table.update_item.return_value = {'Attributes': {'status': 'completed'}}
        repo = _make_repo_with_table(mocker, table)
        result = repo.update_status('bf-abc', 'completed')
        assert result['status'] == 'completed'
        kwargs = table.update_item.call_args.kwargs
        assert kwargs['Key'] == {'execution_name': 'bf-abc'}
        assert ':s' in kwargs['ExpressionAttributeValues']
        assert kwargs['ExpressionAttributeValues'][':s'] == 'completed'

    def test_status_update_with_condition(self, mocker):
        """Cancel flow uses condition to atomically check status."""
        table = mocker.MagicMock()
        table.update_item.return_value = {'Attributes': {}}
        repo = _make_repo_with_table(mocker, table)
        repo.update_status('bf-abc', 'canceled',
                           condition_status_in=['pending', 'running'])
        kwargs = table.update_item.call_args.kwargs
        assert 'ConditionExpression' in kwargs
        assert 'IN' in kwargs['ConditionExpression']
        # Both expected values present in attr values
        values = kwargs['ExpressionAttributeValues']
        assert 'pending' in values.values()
        assert 'running' in values.values()


class TestIncrementCounter:
    def test_increment_completed(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.increment_counter('bf-abc', 'completed_partitions')
        kwargs = table.update_item.call_args.kwargs
        assert 'ADD completed_partitions' in kwargs['UpdateExpression']
        assert kwargs['ExpressionAttributeValues'][':inc'] == 1

    def test_increment_failed(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.increment_counter('bf-abc', 'failed_partitions', by=2)
        kwargs = table.update_item.call_args.kwargs
        assert 'ADD failed_partitions' in kwargs['UpdateExpression']
        assert kwargs['ExpressionAttributeValues'][':inc'] == 2

    def test_increment_skipped(self, mocker):
        table = mocker.MagicMock()
        repo = _make_repo_with_table(mocker, table)
        repo.increment_counter('bf-abc', 'skipped_partitions')
        kwargs = table.update_item.call_args.kwargs
        assert 'ADD skipped_partitions' in kwargs['UpdateExpression']

    def test_invalid_counter_name_rejected(self, mocker):
        repo = _make_repo_with_table(mocker, mocker.MagicMock())
        with pytest.raises(ValueError, match="Invalid counter name"):
            repo.increment_counter('bf-abc', 'arbitrary_field')

    def test_invalid_injection_attempt_rejected(self, mocker):
        """Defense against UpdateExpression injection via counter_name."""
        repo = _make_repo_with_table(mocker, mocker.MagicMock())
        with pytest.raises(ValueError):
            repo.increment_counter('bf-abc', 'completed_partitions, status = :evil')


# ──────────────────────────────────────────────────────────────────────────────
# Query ops
# ──────────────────────────────────────────────────────────────────────────────

class TestListRecent:
    def test_returns_only_backfill_records(self, mocker):
        """Sentinel filter ensures non-Backfill rows are not returned."""
        table = mocker.MagicMock()
        # scan returns a mix; FilterExpression filters server-side, but
        # the mock can just return what we want as already-filtered.
        table.scan.return_value = {
            'Items': [
                {'execution_name': 'bf-x', 'started_at': '2024-01-15T10:00:00Z'},
                {'execution_name': 'bf-y', 'started_at': '2024-01-16T10:00:00Z'},
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_recent(limit=10)
        # Sorted desc by started_at
        assert [r['execution_name'] for r in result] == ['bf-y', 'bf-x']

    def test_respects_limit(self, mocker):
        table = mocker.MagicMock()
        table.scan.return_value = {
            'Items': [
                {'execution_name': f'bf-{i}', 'started_at': f'2024-01-{i:02d}T00:00:00Z'}
                for i in range(1, 6)
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_recent(limit=3)
        assert len(result) == 3

    def test_missing_started_at_sorts_last(self, mocker):
        table = mocker.MagicMock()
        table.scan.return_value = {
            'Items': [
                {'execution_name': 'bf-a', 'started_at': '2024-01-15T10:00:00Z'},
                {'execution_name': 'bf-b'},  # missing started_at
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_recent(limit=10)
        assert result[0]['execution_name'] == 'bf-a'
        assert result[1]['execution_name'] == 'bf-b'


class TestListRecentCursor:
    """`before` — the History feed's started_at cursor (console_api/feed.py, ADR #95)."""

    def _table(self, mocker, count=5):
        table = mocker.MagicMock()
        table.scan.return_value = {
            'Items': [
                {'execution_name': f'bf-{i}', 'started_at': f'2024-01-{i:02d}T00:00:00Z'}
                for i in range(1, count + 1)
            ]
        }
        return table

    def test_returns_only_backfills_older_than_the_cursor(self, mocker):
        repo = _make_repo_with_table(mocker, self._table(mocker))
        result = repo.list_recent(limit=10, before='2024-01-03T00:00:00Z')
        assert [r['execution_name'] for r in result] == ['bf-2', 'bf-1']

    def test_the_cursor_row_itself_is_not_re_served(self, mocker):
        """Strict `<`: the last row of the previous page must not come back."""
        repo = _make_repo_with_table(mocker, self._table(mocker))
        result = repo.list_recent(limit=10, before='2024-01-03T00:00:00Z')
        assert 'bf-3' not in [r['execution_name'] for r in result]

    def test_cursor_is_applied_before_the_limit(self, mocker):
        """The regression this exists to prevent: with the cursor applied *after*
        the slice, the newest `limit` rows are all that survive to be filtered — so
        every page but the first comes back empty and Backfills vanish from the feed
        the moment you page."""
        repo = _make_repo_with_table(mocker, self._table(mocker))
        result = repo.list_recent(limit=2, before='2024-01-03T00:00:00Z')
        assert [r['execution_name'] for r in result] == ['bf-2', 'bf-1']

    def test_no_cursor_is_unchanged_behaviour(self, mocker):
        repo = _make_repo_with_table(mocker, self._table(mocker))
        assert len(repo.list_recent(limit=10)) == 5

    def test_missing_started_at_counts_as_oldest(self, mocker):
        """It sorts last, so the cursor must keep it rather than drop it — which is
        also why this filter stays in memory instead of becoming a FilterExpression."""
        table = mocker.MagicMock()
        table.scan.return_value = {'Items': [
            {'execution_name': 'bf-a', 'started_at': '2024-01-15T10:00:00Z'},
            {'execution_name': 'bf-b'},
        ]}
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_recent(limit=10, before='2024-01-15T10:00:00Z')
        assert [r['execution_name'] for r in result] == ['bf-b']


class TestListActive:
    def test_filters_to_pending_and_running(self, mocker):
        table = mocker.MagicMock()
        # FilterExpression handles server-side; mock returns pre-filtered
        table.scan.return_value = {
            'Items': [
                {'execution_name': 'bf-x', 'status': 'running'},
                {'execution_name': 'bf-y', 'status': 'pending'},
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_active()
        statuses = {r['status'] for r in result}
        assert statuses == {'pending', 'running'}


class TestQueryChildExecutions:
    def test_uses_backfill_id_index(self, mocker):
        table = mocker.MagicMock()
        table.query.return_value = {
            'Items': [
                {'execution_name': 'task-2024-01-15-abc', 'backfill_id': 'bf-x'},
                {'execution_name': 'task-2024-01-16-abc', 'backfill_id': 'bf-x'},
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.query_child_executions('bf-x')
        assert len(result) == 2
        kwargs = table.query.call_args.kwargs
        assert kwargs['IndexName'] == 'backfill-id-index'

    def test_returns_empty_for_unknown_backfill(self, mocker):
        table = mocker.MagicMock()
        table.query.return_value = {'Items': []}
        repo = _make_repo_with_table(mocker, table)
        assert repo.query_child_executions('bf-nonexistent') == []


class TestListRetriesOf:
    """v0.78.11, ADR #68 — retry chain helper."""

    def test_filters_by_parent_backfill_id(self, mocker):
        table = mocker.MagicMock()
        table.scan.return_value = {
            'Items': [
                {'execution_name': 'bf-retry1', 'parent_backfill_id': 'bf-parent',
                 'started_at': '2024-01-15T10:00:00Z'},
                {'execution_name': 'bf-retry2', 'parent_backfill_id': 'bf-parent',
                 'started_at': '2024-01-15T11:00:00Z'},
            ]
        }
        repo = _make_repo_with_table(mocker, table)
        result = repo.list_retries_of('bf-parent')
        assert len(result) == 2
        # Sorted by started_at asc (chronological retry order)
        assert result[0]['execution_name'] == 'bf-retry1'
        assert result[1]['execution_name'] == 'bf-retry2'

    def test_returns_empty_when_no_retries(self, mocker):
        table = mocker.MagicMock()
        table.scan.return_value = {'Items': []}
        repo = _make_repo_with_table(mocker, table)
        assert repo.list_retries_of('bf-orphan') == []

