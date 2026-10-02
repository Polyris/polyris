"""Tests for ExecutionsRepo.batch_get_run_meta.

Verifies the BatchGetItem helper that back-fills triggered_by, attempt, and
task_config on executions fetched from GSIs that do not project these fields.

Import note: `dal.executions_repo` is shadowed in `dal/__init__.py` by the
singleton instance, so we use `importlib.import_module` to get the actual
module object for patching `dynamodb`.
"""
import importlib
import pytest


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


class TestBatchGetRunMeta:
    def test_empty_list_returns_empty_dict_without_ddb_call(self, repo_setup):
        repo, mock_ddb, _ = repo_setup
        assert repo.batch_get_run_meta([]) == {}
        mock_ddb.batch_get_item.assert_not_called()

    def test_blank_strings_return_empty_dict_without_ddb_call(self, repo_setup):
        repo, mock_ddb, _ = repo_setup
        assert repo.batch_get_run_meta(['', '', '']) == {}
        mock_ddb.batch_get_item.assert_not_called()

    def test_returns_full_item_dict_for_found_executions(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a', 'triggered_by': 'console_run',
             'attempt': 1, 'task_config': '{"retries": 2}'},
        ])
        result = repo.batch_get_run_meta(['task-a'])
        assert result['task-a']['triggered_by'] == 'console_run'
        assert result['task-a']['attempt'] == 1
        assert result['task-a']['task_config'] == '{"retries": 2}'

    def test_missing_fields_absent_from_item_dict(self, repo_setup):
        """Fields absent in DDB are simply absent from the returned dict (not None-filled)."""
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a'},
        ])
        result = repo.batch_get_run_meta(['task-a'])
        assert 'triggered_by' not in result['task-a']
        assert 'attempt' not in result['task-a']

    def test_deduplicates_names_before_calling_ddb(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [
            {'execution_name': 'task-a', 'triggered_by': 'asset'},
        ])
        result = repo.batch_get_run_meta(['task-a', 'task-a', 'task-a'])
        assert mock_ddb.batch_get_item.call_count == 1
        keys = mock_ddb.batch_get_item.call_args[1]['RequestItems'][table]['Keys']
        assert len(keys) == 1
        assert result['task-a']['triggered_by'] == 'asset'

    def test_attempt_aliased_in_expression_to_avoid_reserved_word(self, repo_setup):
        """DynamoDB reserves `attempt`; the call must alias it via ExpressionAttributeNames."""
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [])
        repo.batch_get_run_meta(['task-a'])
        call_kwargs = mock_ddb.batch_get_item.call_args[1]['RequestItems'][table]
        assert '#attempt' in call_kwargs['ProjectionExpression']
        assert call_kwargs['ExpressionAttributeNames']['#attempt'] == 'attempt'

    def test_retries_unprocessed_keys_until_empty(self, repo_setup):
        repo, mock_ddb, table = repo_setup
        unprocessed = {table: {
            'Keys': [{'execution_name': 'task-b'}],
            'ProjectionExpression': 'execution_name, triggered_by, #attempt, task_config',
            'ExpressionAttributeNames': {'#attempt': 'attempt'},
        }}
        mock_ddb.batch_get_item.side_effect = [
            _resp(table, [{'execution_name': 'task-a', 'triggered_by': 'schedule'}], unprocessed),
            _resp(table, [{'execution_name': 'task-b', 'triggered_by': 'asset'}]),
        ]
        result = repo.batch_get_run_meta(['task-a', 'task-b'])
        assert mock_ddb.batch_get_item.call_count == 2
        assert result['task-a']['triggered_by'] == 'schedule'
        assert result['task-b']['triggered_by'] == 'asset'

    def test_chunks_at_100_items_aws_limit(self, repo_setup):
        """AWS hard limit: 100 keys per BatchGetItem call."""
        repo, mock_ddb, table = repo_setup
        mock_ddb.batch_get_item.return_value = _resp(table, [])
        repo.batch_get_run_meta([f'task-{i}' for i in range(101)])
        assert mock_ddb.batch_get_item.call_count == 2
        calls = mock_ddb.batch_get_item.call_args_list
        first_keys = calls[0][1]['RequestItems'][table]['Keys']
        second_keys = calls[1][1]['RequestItems'][table]['Keys']
        assert len(first_keys) == 100
        assert len(second_keys) == 1
