"""Tests for ExecutionsRepo.batch_get_triggered_by.

Verifies the single-call BatchGetItem that back-fills triggered_by on
executions fetched from GSIs that do not project the field.

Import note: `dal.executions_repo` is shadowed in `dal/__init__.py` by the
singleton instance, so we use `importlib.import_module` to get the actual
module object for patching `dynamodb`.
"""
import importlib
import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def repo_setup(mocker):
    """Return (repo_instance, mock_ddb, table_name) with dynamodb patched."""
    mod = importlib.import_module('dal.executions_repo')
    from dal.executions_repo import ExecutionsRepo
    repo = ExecutionsRepo()
    mock_ddb = mocker.MagicMock()
    mocker.patch.object(mod, 'dynamodb', mock_ddb)
    return repo, mock_ddb, repo._table_name


def _resp(table, items, unprocessed=None):
    return {'Responses': {table: items}, 'UnprocessedKeys': unprocessed or {}}


class TestBatchGetTriggeredBy:
    def test_empty_list_returns_empty_dict_without_ddb_call(self, repo_setup):
        repo, mock_ddb, _ = repo_setup
        assert repo.batch_get_triggered_by([]) == {}
        mock_ddb.batch_get_item.assert_not_called()

    def test_blank_strings_return_empty_dict_without_ddb_call(self, repo_setup):
        repo, mock_ddb, _ = repo_setup
        assert repo.batch_get_triggered_by(['', '', '']) == {}
        mock_ddb.batch_get_item.assert_not_called()

    def test_returns_triggered_by_for_found_items(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a', 'triggered_by': 'console_run'},
            {'execution_name': 'task-b', 'triggered_by': 'schedule'},
        ])
        result = repo.batch_get_triggered_by(['task-a', 'task-b'])
        assert result == {'task-a': 'console_run', 'task-b': 'schedule'}

    def test_missing_triggered_by_field_maps_to_none(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a'},
        ])
        result = repo.batch_get_triggered_by(['task-a'])
        assert result['task-a'] is None

    def test_deduplicates_names_before_calling_ddb(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a', 'triggered_by': 'asset'},
        ])
        result = repo.batch_get_triggered_by(['task-a', 'task-a', 'task-a'])
        assert mock_ddb.batch_get_item.call_count == 1
        keys = mock_ddb.batch_get_item.call_args[1]['RequestItems'][table]['Keys']
        assert len(keys) == 1
        assert result == {'task-a': 'asset'}

    def test_retries_unprocessed_keys_until_empty(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        unprocessed = {table: {
            'Keys': [{'execution_name': 'task-b'}],
            'ProjectionExpression': 'execution_name, triggered_by',
        }}
        mock_ddb.batch_get_item.side_effect = [
            _resp(table, [{'execution_name': 'task-a', 'triggered_by': 'schedule'}], unprocessed),
            _resp(table, [{'execution_name': 'task-b', 'triggered_by': 'asset'}]),
        ]
        result = repo.batch_get_triggered_by(['task-a', 'task-b'])
        assert mock_ddb.batch_get_item.call_count == 2
        assert result == {'task-a': 'schedule', 'task-b': 'asset'}

    def test_chunks_at_100_items_aws_limit(self, repo_setup):
        """AWS hard limit: 100 keys per BatchGetItem call."""
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [])
        repo.batch_get_triggered_by([f'task-{i}' for i in range(101)])
        assert mock_ddb.batch_get_item.call_count == 2
        calls = mock_ddb.batch_get_item.call_args_list
        first_keys = calls[0][1]['RequestItems'][table]['Keys']
        second_keys = calls[1][1]['RequestItems'][table]['Keys']
        assert len(first_keys) == 100
        assert len(second_keys) == 1
