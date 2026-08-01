"""
Tests for notify_asset_subscribers Lambda.

Tests subscriber notification and freshness re-checking.
"""

import pytest
from datetime import datetime, timezone, timedelta


@pytest.fixture(autouse=True)
def reset_globals(mocker):
    """Reset global clients for each test."""
    import index
    # v0.79.3 (ADR #75): _dynamodb global removed; DAL repos are
    # module singletons but tests patch their methods directly per test.
    mocker.patch.object(index, '_sfn', None)


class TestNotifyAssetSubscribers:
    """Test notify_asset_subscribers handler."""
    
    def test_empty_outlets_returns_zero(self):
        """Empty outlets should return notified=0."""
        from index import handler
        
        result = handler({'outlets': []}, None)
        
        assert result['notified'] == 0
        assert result['assets'] == []
        assert result['subscribers'] == []
    
    def test_no_subscribers_returns_zero(self, mocker):
        """No subscribers should return notified=0."""
        from index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': []}
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'source_task': 'producer',
            'source_dag': 'pipeline',
            'event_time': datetime.now(timezone.utc).isoformat()
        }, None)
        
        assert result['notified'] == 0
        assert 'inventory' in result['assets']
    
    def test_notifies_subscribers(self, mocker):
        """Should notify waiting subscribers."""
        from index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [
                {'subscriber': 'task_a', 'wait_token': 'token_a'},
                {'subscriber': 'task_b', 'wait_token': 'token_b'},
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mock_sfn = mocker.MagicMock()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'source_task': 'producer',
            'source_dag': 'pipeline',
            'event_time': datetime.now(timezone.utc).isoformat()
        }, None)
        
        assert result['notified'] == 2
        assert 'task_a' in result['subscribers']
        assert 'task_b' in result['subscribers']
        assert mock_sfn.send_task_success.call_count == 2
        assert mock_table.delete_item.call_count == 2
    
    def test_handles_expired_token(self, mocker):
        """Should handle expired token gracefully."""
        from index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [{'subscriber': 'task_a', 'wait_token': 'expired_token'}]
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mock_sfn = mocker.MagicMock()
        mock_sfn.exceptions = mocker.MagicMock()
        mock_sfn.exceptions.TaskTimedOut = Exception
        mock_sfn.send_task_success.side_effect = mock_sfn.exceptions.TaskTimedOut()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'source_task': 'producer',
            'source_dag': 'pipeline',
            'event_time': datetime.now(timezone.utc).isoformat()
        }, None)
        
        assert result['notified'] == 0
    
    def test_skips_stale_for_freshness_subscriber(self, mocker):
        """Should skip notification if subscriber requires freshness and asset is stale."""
        from index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [
                {'subscriber': 'task_a', 'wait_token': 'token_a', 'freshness_hours': 1}
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mock_sfn = mocker.MagicMock()
        
        stale_event_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'source_task': 'producer',
            'source_dag': 'pipeline',
            'event_time': stale_event_time
        }, None)
        
        assert result['notified'] == 0
        mock_sfn.send_task_success.assert_not_called()
    
    def test_multiple_outlets(self, mocker):
        """Should handle multiple outlets."""
        from index import handler
        
        mock_table = mocker.MagicMock()
        
        def mock_query(**kwargs):
            key = kwargs.get('ExpressionAttributeValues', {}).get(':dk', '')
            if 'inventory' in key:
                return {'Items': [{'subscriber': 'task_inv', 'wait_token': 'token_inv'}]}
            elif 'catalog' in key:
                return {'Items': [{'subscriber': 'task_cat', 'wait_token': 'token_cat'}]}
            return {'Items': []}
        
        mock_table.query.side_effect = mock_query
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mock_sfn = mocker.MagicMock()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [
                {'name': 'inventory', 'uri': 's3://bucket/inv'},
                {'name': 'catalog', 'uri': 's3://bucket/cat'}
            ],
            'source_task': 'producer',
            'source_dag': 'pipeline',
            'event_time': datetime.now(timezone.utc).isoformat()
        }, None)
        
        assert result['notified'] == 2
        assert 'inventory' in result['assets']
        assert 'catalog' in result['assets']


class TestHelperFunctions:
    """Test helper functions."""
    
    def test_check_freshness_fresh(self):
        """Fresh event should return True."""
        from index import _check_freshness
        
        event_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert _check_freshness('inventory', event_time, 24) is True
    
    def test_check_freshness_stale(self):
        """Stale event should return False."""
        from index import _check_freshness
        
        event_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        assert _check_freshness('inventory', event_time, 24) is False
    
    def test_get_asset_subscribers(self, mocker):
        """Should query subscribers by asset key."""
        from index import _get_asset_subscribers
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [
                {'subscriber': 'task_a', 'wait_token': 'token_a'}
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — also wire mock_table into DAL repos
        # so calls via subscriptions_repo / asset_events_repo land
        # on the same mock the test set up.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _get_asset_subscribers('inventory')
        
        assert len(result) == 1
        assert result[0]['subscriber'] == 'task_a'
        mock_table.query.assert_called_once()
        call_kwargs = mock_table.query.call_args[1]
        assert ':dk' in str(call_kwargs)


class TestConsecutiveNotify:
    """Test consecutive re-check in notify."""
    
    def test_consecutive_not_ready_skips_signal(self, mocker):
        """3/7 consecutive days → don't send signal, keep subscription."""
        from index import handler
        
        mock_sub_table = mocker.MagicMock()
        mock_sub_table.query.return_value = {
            'Items': [{
                'subscriber': 'weekly_task',
                'wait_token': 'token123',
                'subscription_type': 'asset_consecutive',
                'consecutive_days': 7,
                'reference_date': '2026-02-22'
            }]
        }
        
        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {
            'Items': [
                {'asset_name': 'daily', 'execution_date': '2026-02-20', 'event_time': '2026-02-20T08:00:00Z'},
                {'asset_name': 'daily', 'execution_date': '2026-02-21', 'event_time': '2026-02-21T08:00:00Z'},
                {'asset_name': 'daily', 'execution_date': '2026-02-22', 'event_time': '2026-02-22T08:00:00Z'},
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        def table_router(name):
            if 'subscription' in name or 'dependency' in name:
                return mock_sub_table
            return mock_events_table
        mock_dynamodb.Table.side_effect = table_router
        
        mock_sfn = mocker.MagicMock()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — wire the per-table mocks into the DAL repos.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'daily', 'uri': 's3://bucket/daily'}],
            'source_task': 'daily_task',
            'source_dag': 'daily_pipeline',
            'event_time': '2026-02-22T08:00:00Z'
        }, None)
        
        assert result['notified'] == 0
        mock_sfn.send_task_success.assert_not_called()
        mock_sub_table.delete_item.assert_not_called()
    
    def test_consecutive_ready_sends_signal(self, mocker):
        """7/7 consecutive days → send signal."""
        from index import handler
        
        mock_sub_table = mocker.MagicMock()
        mock_sub_table.query.return_value = {
            'Items': [{
                'subscriber': 'weekly_task',
                'wait_token': 'token123',
                'subscription_type': 'asset_consecutive',
                'consecutive_days': 7,
                'reference_date': '2026-02-22'
            }]
        }
        
        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {
            'Items': [
                {'asset_name': 'daily', 'execution_date': f'2026-02-{16+i:02d}', 'event_time': f'2026-02-{16+i:02d}T08:00:00Z'}
                for i in range(7)
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        def table_router(name):
            if 'subscription' in name or 'dependency' in name:
                return mock_sub_table
            return mock_events_table
        mock_dynamodb.Table.side_effect = table_router
        
        mock_sfn = mocker.MagicMock()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — wire the per-table mocks into the DAL repos.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'daily', 'uri': 's3://bucket/daily'}],
            'source_task': 'daily_task',
            'source_dag': 'daily_pipeline',
            'event_time': '2026-02-22T08:00:00Z'
        }, None)
        
        assert result['notified'] == 1
        assert 'weekly_task' in result['subscribers']
        mock_sfn.send_task_success.assert_called_once()
    
    def test_mixed_within_and_consecutive(self, mocker):
        """within subscriber gets signal, consecutive (not ready) doesn't."""
        from index import handler
        
        mock_sub_table = mocker.MagicMock()
        mock_sub_table.query.return_value = {
            'Items': [
                {
                    'subscriber': 'within_task',
                    'wait_token': 'token_within',
                    'subscription_type': 'asset',
                },
                {
                    'subscriber': 'consec_task',
                    'wait_token': 'token_consec',
                    'subscription_type': 'asset_consecutive',
                    'consecutive_days': 7,
                    'reference_date': '2026-02-22'
                }
            ]
        }
        
        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {
            'Items': [
                {'asset_name': 'daily', 'execution_date': '2026-02-20', 'event_time': '2026-02-20T08:00:00Z'},
                {'asset_name': 'daily', 'execution_date': '2026-02-21', 'event_time': '2026-02-21T08:00:00Z'},
                {'asset_name': 'daily', 'execution_date': '2026-02-22', 'event_time': '2026-02-22T08:00:00Z'},
            ]
        }
        
        mock_dynamodb = mocker.MagicMock()
        def table_router(name):
            if 'subscription' in name or 'dependency' in name:
                return mock_sub_table
            return mock_events_table
        mock_dynamodb.Table.side_effect = table_router
        
        mock_sfn = mocker.MagicMock()
        
        mocker.patch('index._get_dynamodb', return_value=mock_dynamodb, create=True)
        # v0.79.3 (ADR #75) — wire the per-table mocks into the DAL repos.
        from index import subscriptions_repo, asset_events_repo
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch('index._get_sfn', return_value=mock_sfn)
        result = handler({
            'outlets': [{'name': 'daily', 'uri': 's3://bucket/daily'}],
            'source_task': 'daily_task',
            'source_dag': 'daily_pipeline',
            'event_time': '2026-02-22T08:00:00Z'
        }, None)
        
        assert result['notified'] == 1
        assert 'within_task' in result['subscribers']
        assert 'consec_task' not in result['subscribers']


class TestCoordinateReadyCheck:
    """Case B (Option J): notify_asset_subscribers must delegate the signal
    decision to evaluate_deps when the subscriber has task_deps that may not
    yet be satisfied. Without coordination, an asset arriving before the last
    task_dep completes would wake dep_wrapper prematurely and the task would
    xcom.pull() from pending upstreams."""

    def test_coordinate_returns_true_when_no_execution_name(self, mocker):
        """Legacy subscriptions (missing execution_name) fall back to the old
        'always signal' behaviour — worst case they trigger the original
        Case B early-run, which is a strict improvement over hanging."""
        from index import _coordinate_ready_check
        assert _coordinate_ready_check('') is True

    def test_coordinate_returns_true_when_record_not_found(self, mocker):
        """Task record missing (TTL'd, cross-account leak, whatever) → fall
        back to signal. Better a stale signal than a permanent hang."""
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'mark_assets_ready_and_get', return_value=None)
        assert _coordinate_ready_check('missing-execution-name') is True

    def test_coordinate_returns_true_when_evaluate_deps_not_configured(self, mocker):
        """A fresh deploy might roll out the code before EVALUATE_DEPS_LAMBDA
        is wired. Signal anyway rather than block."""
        import index
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'mark_assets_ready_and_get',
                            return_value={'dependencies': '[]', 'wait_for': '[]'})
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
        assert _coordinate_ready_check('some-execution') is True

    def test_coordinate_signals_when_evaluate_deps_says_ready(self, mocker):
        """The happy path: both task_deps and assets are now ready → signal."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'mark_assets_ready_and_get',
                            return_value={
                                'dependencies': '["upstream_a"]',
                                'trigger_rule': 'all_success',
                                'date': '2026-07-27',
                                'pipeline_execution_short': 'run-abc',
                                'pipeline_execution': 'pipe-run-abc',
                                'wait_for': '[{"asset_name": "inventory"}]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.return_value = {
            'Payload': mocker.MagicMock(read=lambda: json.dumps({'is_ready': True}).encode())
        }
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        assert _coordinate_ready_check('exec-1') is True

    def test_coordinate_does_not_signal_when_task_deps_still_pending(self, mocker):
        """The core Case B fix: asset arrives but task_deps haven't finished.
        evaluate_deps returns is_ready=False → skip the signal so
        notify_dependents will signal later once task_deps complete."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'mark_assets_ready_and_get',
                            return_value={
                                'dependencies': '["still_pending_upstream"]',
                                'trigger_rule': 'all_success',
                                'wait_for': '[{"asset_name": "inventory"}]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.return_value = {
            'Payload': mocker.MagicMock(
                read=lambda: json.dumps({'is_ready': False, 'verdict': 'wait'}).encode())
        }
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        assert _coordinate_ready_check('exec-1') is False

    def test_coordinate_falls_back_to_true_on_invoke_failure(self, mocker):
        """Lambda invoke error → fall back to signaling. Case B is a
        correctness improvement, not a hard invariant — a transient invoke
        failure must not hang the task."""
        import index
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'mark_assets_ready_and_get',
                            return_value={'dependencies': '[]', 'wait_for': '[]'})
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.side_effect = Exception('Lambda unavailable')
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        assert _coordinate_ready_check('exec-1') is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
