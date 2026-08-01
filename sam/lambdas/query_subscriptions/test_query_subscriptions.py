"""Tests for query_subscriptions Lambda (v0.79.3, ADR #75)."""
import os
import pytest


@pytest.fixture(autouse=True)
def setup_env():
    os.environ['SUBSCRIPTIONS_TABLE'] = 'asset-subscriptions-test'


class TestHandler:
    def test_missing_task_name_returns_error(self):
        """ValueError is caught and returned in `error` field, not raised."""
        from index import handler
        result = handler({'pipeline_execution_short': 'abc123'}, None)
        assert result['subscribers'] == []
        assert 'Missing required fields' in result['error']

    def test_missing_pipeline_execution_short_returns_error(self):
        from index import handler
        result = handler({'task_name': 'producer'}, None)
        assert result['subscribers'] == []
        assert 'Missing required fields' in result['error']

    def test_returns_subscribers_from_repo(self, mocker):
        from index import handler
        mocker.patch(
            'index.subscriptions_repo.list_for_dependency',
            return_value=[
                {'subscriber': 'task_a', 'wait_token': 'tok_a'},
                {'subscriber': 'task_b', 'wait_token': 'tok_b'},
            ],
        )
        result = handler({
            'task_name': 'producer',
            'pipeline_execution_short': 'abc123',
        }, None)
        assert result['subscribers'] == [
            {'subscriber': 'task_a', 'wait_token': 'tok_a'},
            {'subscriber': 'task_b', 'wait_token': 'tok_b'},
        ]

    def test_empty_subscribers_returns_empty_list(self, mocker):
        from index import handler
        mocker.patch(
            'index.subscriptions_repo.list_for_dependency', return_value=[]
        )
        result = handler({
            'task_name': 'producer',
            'pipeline_execution_short': 'abc123',
        }, None)
        assert result['subscribers'] == []

    def test_access_denied_re_raises(self, mocker):
        """Permission errors must propagate so SFN Retry can catch."""
        from index import handler
        mocker.patch(
            'index.subscriptions_repo.list_for_dependency',
            side_effect=Exception('AccessDeniedException: not allowed'),
        )
        with pytest.raises(Exception, match='AccessDenied'):
            handler({
                'task_name': 'producer',
                'pipeline_execution_short': 'abc123',
            }, None)

    def test_other_errors_swallowed_with_error_field(self, mocker):
        """Non-permission errors are returned, not raised (keeps SFN moving)."""
        from index import handler
        mocker.patch(
            'index.subscriptions_repo.list_for_dependency',
            side_effect=Exception('ResourceNotFound: table missing'),
        )
        result = handler({
            'task_name': 'producer',
            'pipeline_execution_short': 'abc123',
        }, None)
        assert result['subscribers'] == []
        assert 'ResourceNotFound' in result['error']

    def test_dependency_key_format(self, mocker):
        from index import handler
        mock_list = mocker.patch(
            'index.subscriptions_repo.list_for_dependency', return_value=[]
        )
        handler({
            'task_name': 'producer',
            'pipeline_execution_short': 'abc123',
        }, None)
        # Verify the key was constructed as {task_name}-{pipeline_execution_short}
        assert mock_list.call_args.args[0] == 'producer-abc123'


class TestSubscriptionsRepo:
    def test_table_name_read_from_env_lazily(self, monkeypatch):
        from qs_dal import SubscriptionsRepo
        # Repo doesn't bind env at construction
        repo = SubscriptionsRepo()
        monkeypatch.setenv('SUBSCRIPTIONS_TABLE', 'test-table-name')
        assert repo.table_name == 'test-table-name'

    def test_explicit_table_name_overrides_env(self, monkeypatch):
        from qs_dal import SubscriptionsRepo
        monkeypatch.setenv('SUBSCRIPTIONS_TABLE', 'env-table')
        repo = SubscriptionsRepo(table_name='explicit-table')
        assert repo.table_name == 'explicit-table'

    def test_list_handles_single_page(self, mocker):
        from qs_dal import SubscriptionsRepo
        repo = SubscriptionsRepo(table_name='t')
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [{'subscriber': 'a'}, {'subscriber': 'b'}],
        }  # no LastEvaluatedKey
        mocker.patch.object(type(repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        items = repo.list_for_dependency('producer-abc123')
        assert len(items) == 2

    def test_list_handles_pagination(self, mocker):
        from qs_dal import SubscriptionsRepo
        repo = SubscriptionsRepo(table_name='t')
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [{'subscriber': 'a'}], 'LastEvaluatedKey': {'k': 1}},
            {'Items': [{'subscriber': 'b'}]},  # no LastEvaluatedKey → stop
        ]
        mocker.patch.object(type(repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        items = repo.list_for_dependency('producer-abc123')
        assert len(items) == 2
        assert mock_table.query.call_count == 2
