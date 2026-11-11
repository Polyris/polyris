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
        should_signal, record = _coordinate_ready_check('', 'inventory')
        assert should_signal is True

    def test_coordinate_returns_true_when_record_not_found(self, mocker):
        """Task record missing (TTL'd, cross-account leak, whatever) → fall
        back to signal. Better a stale signal than a permanent hang."""
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get', return_value=None)
        should_signal, record = _coordinate_ready_check('missing-execution-name', 'inventory')
        assert should_signal is True

    def test_coordinate_returns_true_when_evaluate_deps_not_configured(self, mocker):
        """A fresh deploy might roll out the code before EVALUATE_DEPS_LAMBDA
        is wired. Signal anyway rather than block."""
        import index
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={'dependencies': '[]', 'wait_for': '[]'})
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
        should_signal, record = _coordinate_ready_check('some-execution', 'inventory')
        assert should_signal is True

    def test_coordinate_signals_when_evaluate_deps_says_ready(self, mocker):
        """The happy path: both task_deps and assets are now ready → signal."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
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
        should_signal, record = _coordinate_ready_check('exec-1', 'inventory')
        assert should_signal is True

    def test_coordinate_does_not_signal_when_task_deps_still_pending(self, mocker):
        """The core Case B fix: asset arrives but task_deps haven't finished.
        evaluate_deps returns is_ready=False → skip the signal so
        notify_dependents will signal later once task_deps complete."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
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
        should_signal, record = _coordinate_ready_check('exec-1', 'inventory')
        assert should_signal is False

    def test_coordinate_falls_back_to_true_on_invoke_failure(self, mocker):
        """Lambda invoke error → fall back to signaling. Case B is a
        correctness improvement, not a hard invariant — a transient invoke
        failure must not hang the task."""
        import index
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={'dependencies': '[]', 'wait_for': '[]'})
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.side_effect = Exception('Lambda unavailable')
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        should_signal, record = _coordinate_ready_check('exec-1', 'inventory')
        assert should_signal is True

    def test_coordinate_returns_false_when_waiting_for_another_asset(self, mocker):
        """AND-gate: asset 'a' arrives, but 'b' still pending. The set
        check wait_for_assets ⊄ assets_ready_set → don't signal yet. The
        second asset's notify call will find both sets equal and proceed."""
        import index
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={
                                'wait_for_assets': {'a', 'b'},
                                'assets_ready_set': {'a'},
                                'dependencies': '[]',
                                'wait_for': '[]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        should_signal, record = _coordinate_ready_check('exec-1', 'a')
        assert should_signal is False

    def test_coordinate_proceeds_when_all_assets_satisfied(self, mocker):
        """AND-gate: all required assets in assets_ready_set → pass through
        to evaluate_deps which checks task_deps and returns the final verdict."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={
                                'wait_for_assets': {'a', 'b'},
                                'assets_ready_set': {'a', 'b'},
                                'dependencies': '[]',
                                'trigger_rule': 'all_success',
                                'date': '2026-07-27',
                                'pipeline_execution_short': 'run-abc',
                                'pipeline_execution': 'pipe-run-abc',
                                'wait_for': '[]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.return_value = {
            'Payload': mocker.MagicMock(read=lambda: json.dumps({'is_ready': True}).encode())
        }
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        should_signal, record = _coordinate_ready_check('exec-1', 'b')
        assert should_signal is True


class TestOrGroupSignaling:
    """AND-gate must be bypassed when wait_for_assets is absent on the record
    (the case for OR-group subscriptions, where check_assets intentionally
    skips _write_pending_assets).

    Regression guard: before this guard, _coordinate_ready_check would call
    add_to_assets_ready_set_and_get, find wait_for_assets absent → no AND gate
    → pass through to evaluate_deps with wait_for_assets=[].  evaluate_deps:
    set([]) <= set(anything) = True → assets_satisfied=True → signal.
    This is the correct first-arrival (OR) behaviour."""

    def test_or_group_first_asset_bypasses_and_gate_and_signals(self, mocker):
        """Record has no wait_for_assets (OR group subscription) — AND gate is
        skipped and evaluate_deps sees assets_satisfied=True on first arrival."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo
        # Record after ADD: assets_ready_set={'manual_override'}, no wait_for_assets
        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={
                                'assets_ready_set': {'manual_override'},
                                # wait_for_assets absent — OR group, check_assets skipped write
                                'dependencies': '[]',
                                'trigger_rule': 'all_success',
                                'date': '2026-07-27',
                                'pipeline_execution_short': 'run-abc',
                                'pipeline_execution': 'pipe-run-abc',
                                'wait_for': '[{"operator":"OR","assets":[{"asset_name":"daily"},{"asset_name":"manual_override"}]}]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.return_value = {
            'Payload': mocker.MagicMock(
                read=lambda: json.dumps({'is_ready': True}).encode())
        }
        mocker.patch('index._get_lambda', return_value=fake_lambda)
        # First asset arrives → no AND gate → evaluate_deps called → signal
        should_signal, record = _coordinate_ready_check('exec-1', 'manual_override')
        assert should_signal is True

    def test_or_group_evaluate_deps_receives_null_wait_for_assets(self, mocker):
        """evaluate_deps payload has wait_for_assets=None when record lacks the
        field. None signals "no AND gate" so evaluate_deps uses the legacy
        assets_ready bool — OR-group and legacy rows wake immediately."""
        import index
        import json
        from index import _coordinate_ready_check, tokens_repo

        mocker.patch.object(tokens_repo, 'add_to_assets_ready_set_and_get',
                            return_value={
                                'assets_ready_set': {'manual_override'},
                                'dependencies': '[]',
                                'trigger_rule': 'all_success',
                                'date': '2026-07-27',
                                'pipeline_execution_short': 'run-abc',
                                'pipeline_execution': 'pipe-run-abc',
                                'wait_for': '[]',
                            })
        mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', 'arn:evaluate-deps')
        fake_lambda = mocker.MagicMock()
        captured = {}

        def fake_invoke(**kwargs):
            captured['payload'] = json.loads(kwargs['Payload'])
            return {'Payload': mocker.MagicMock(
                read=lambda: json.dumps({'is_ready': True}).encode())}

        fake_lambda.invoke.side_effect = fake_invoke
        mocker.patch('index._get_lambda', return_value=fake_lambda)

        _coordinate_ready_check('exec-1', 'manual_override')

        assert captured['payload']['wait_for_assets'] is None, (
            "wait_for_assets must be None (not []) when absent so evaluate_deps "
            "treats it as 'no gate' rather than 'empty gate'"
        )
        assert captured['payload']['assets_ready'] is True


class TestSignalFailureWarn:
    """B1: when send_task_success fails (transient / IAM), subscription is kept
    for retry and a _notify_warn_ record is written so the operator sees it.

    Subscription must NOT be deleted on 'failed' — only on 'sent' or
    'token_consumed'. This is the inverse of the pre-B1 bug where the
    subscription was deleted first, leaving no retry path on signal failure."""

    def test_signal_failure_keeps_subscription_and_writes_warn(self, mocker):
        """IAM / transient failure from SFN: subscription kept, _notify_warn_ written,
        subscriber not counted in notified_count."""
        from index import handler, subscriptions_repo, tokens_repo

        record = {
            'execution_name': 'task-2026-01-01-abc',
            'pipeline_execution': 'pipe-run-2026-01-01-abc',
            'pipeline_name': 'my-pipeline',
            'date': '2026-01-01',
        }
        mocker.patch.object(subscriptions_repo, 'list_for_asset', return_value=[{
            'subscriber': 'task_a',
            'wait_token': 'token_a',
            'execution_name': 'task-2026-01-01-abc',
        }])
        mocker.patch('index._coordinate_ready_check', return_value=(True, record))

        mock_sfn = mocker.MagicMock()
        mock_sfn.exceptions.TaskTimedOut = type('TaskTimedOut', (Exception,), {})
        mock_sfn.exceptions.InvalidToken = type('InvalidToken', (Exception,), {})
        mock_sfn.exceptions.TaskDoesNotExist = type('TaskDoesNotExist', (Exception,), {})
        mock_sfn.send_task_success.side_effect = Exception('AccessDeniedException')
        mocker.patch('index._get_sfn', return_value=mock_sfn)

        warn_written = {}
        def capture_write_warn(**kwargs):
            warn_written.update(kwargs)
        mocker.patch.object(tokens_repo, 'write_warn', side_effect=capture_write_warn)

        delete_mock = mocker.patch.object(subscriptions_repo, 'delete')

        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'event_time': '2026-01-01T00:00:00Z',
        }, None)

        assert result['notified'] == 0, "failed signal must not increment notified_count"
        delete_mock.assert_not_called(), "subscription must be kept for retry on signal failure"
        assert warn_written, "_notify_warn_ must be written so operator sees the failure"
        assert warn_written.get('execution_name') == 'task-2026-01-01-abc'
        assert 'inventory' in warn_written.get('error', '')

    def test_token_consumed_deletes_subscription_without_warn(self, mocker):
        """Token already consumed (timeout/task gone): subscription deleted silently,
        no _notify_warn_ (operator cannot act on a gone token)."""
        from index import handler, subscriptions_repo, tokens_repo

        record = {'execution_name': 'task-2026-01-01-abc', 'pipeline_execution': 'p',
                  'pipeline_name': 'p', 'date': '2026-01-01'}
        mocker.patch.object(subscriptions_repo, 'list_for_asset', return_value=[{
            'subscriber': 'task_a',
            'wait_token': 'old_token',
            'execution_name': 'task-2026-01-01-abc',
        }])
        mocker.patch('index._coordinate_ready_check', return_value=(True, record))

        mock_sfn = mocker.MagicMock()
        mock_sfn.exceptions.TaskTimedOut = type('TaskTimedOut', (Exception,), {})
        mock_sfn.exceptions.InvalidToken = type('InvalidToken', (Exception,), {})
        mock_sfn.exceptions.TaskDoesNotExist = type('TaskDoesNotExist', (Exception,), {})
        mock_sfn.send_task_success.side_effect = mock_sfn.exceptions.TaskTimedOut()
        mocker.patch('index._get_sfn', return_value=mock_sfn)

        warn_mock = mocker.patch.object(tokens_repo, 'write_warn')
        delete_mock = mocker.patch.object(subscriptions_repo, 'delete')

        result = handler({
            'outlets': [{'name': 'inventory', 'uri': 's3://bucket/inv'}],
            'event_time': '2026-01-01T00:00:00Z',
        }, None)

        assert result['notified'] == 0
        delete_mock.assert_called_once(), "token_consumed must still delete subscription"
        warn_mock.assert_not_called(), "no _notify_warn_ for consumed tokens"


class TestDynamoDBExpressions:
    """Verify DAL DDB expressions with moto — actual expression semantics.

    Principle #14: mock at the external boundary (boto3 via moto), never at
    internal logic. Catches regressions where assets_ready=true is dropped from
    the UpdateExpression — the Case B path (task deps not yet satisfied when asset
    arrives) requires assets_ready=true in the DDB record so notify_dependents SFN
    can read it later and pass it to evaluate_deps.

    Also covers write_warn (PutItem) — mocking it would not catch a missing IAM
    PutItem permission; a real DDB write through moto does (Principle #13).
    """

    def test_write_warn_puts_notify_warn_record(self, monkeypatch):
        """write_warn must issue a real put_item to PipelineTokensTable.

        Principle #13: mocking write_warn in handler-level tests verifies the
        call signature but not the DDB operation. A missing dynamodb:PutItem
        in NotifyAssetSubscribersRole would only surface here, not in the
        handler mock test. The IAM permission is pinned by
        test_mark_success_wakeup_wiring.py::
        test_notify_asset_subscribers_role_grants_put_item_on_tokens_table.
        """
        from moto import mock_aws
        import boto3
        import dal as dal_module
        from dal import TokensRepo

        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal_module, '_dynamodb', None)

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            dynamodb.create_table(
                TableName='pipeline-tokens-warn',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )

            repo = TokensRepo('pipeline-tokens-warn')
            repo.write_warn(
                execution_name='task-2026-01-01-abc1',
                task_name='my_task',
                pipeline_execution='pipe-run-2026-01-01-abc1',
                pipeline_name='my-pipeline',
                date='2026-01-01',
                error='send_task_success failed for asset "inventory"; subscription kept',
            )

            table = dynamodb.Table('pipeline-tokens-warn')
            item = table.get_item(
                Key={'execution_name': '_notify_warn_task-2026-01-01-abc1'}
            ).get('Item')

        assert item is not None, "_notify_warn_ record must be written to DDB"
        assert item['task_name'] == 'my_task'
        assert item['status'] == 'failed'
        assert 'inventory' in item['error']
        assert 'finished_at' in item
        assert 'ttl' in item

    def test_assets_ready_bool_reaches_evaluate_deps_via_ddb(self, monkeypatch):
        """DAL ADD writes assets_ready=true to DDB; that bool must survive a DDB
        read-back and reach evaluate_deps so the Case B path works.

        Case B: asset arrives while task deps are still pending. notify_asset_subscribers
        credits the asset via ADD (assets_ready=true, assets_ready_set). Later,
        notify_dependents SFN reads the record and passes assets_ready to evaluate_deps.
        If assets_ready=true is absent, evaluate_deps sees assets_ready=False and
        returns is_ready=False — the task hangs to orchestration timeout.

        The test omits task deps (dependencies=[]) to isolate the asset-ready path;
        wait_for_assets is absent (no AND gate) so evaluate_deps uses the legacy bool.
        """
        import os
        import json
        import importlib.util
        from moto import mock_aws
        import boto3
        import dal as dal_module
        from dal import TokensRepo

        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal_module, '_dynamodb', None)

        # monkeypatch.syspath_prepend restores sys.path on teardown even if the test fails.
        eval_deps_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), '..', 'evaluate_deps')
        )
        monkeypatch.syspath_prepend(eval_deps_path)

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-chain',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            exec_name = 'task-2026-01-01-chain01'
            tokens_tbl.put_item(Item={
                'execution_name': exec_name,
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'wait_for': '["some_asset"]',
                'date': '2026-01-01',
                'pipeline_execution_short': 'run-abc',
                'pipeline_execution': 'pipeline-run-abc',
            })

            repo = TokensRepo('pipeline-tokens-chain')
            record = repo.add_to_assets_ready_set_and_get(exec_name, 'some_asset')
            assert record is not None

        # Build the payload exactly as notify_dependents SFN + _coordinate_ready_check do:
        # wait_for_assets absent in record → None; assets_ready=true from DAL write
        payload = {
            'dependencies': [],
            'trigger_rule': 'all_success',
            'date': '2026-01-01',
            'pipeline_execution_short': 'run-abc',
            'pipeline_execution': 'pipeline-run-abc',
            'wait_for': json.loads(record.get('wait_for', '["some_asset"]')),
            'wait_for_assets': (list(record['wait_for_assets']) if 'wait_for_assets' in record else None),
            'assets_ready_set': list(record.get('assets_ready_set') or []),
            'assets_ready': bool(record.get('assets_ready', False)),
        }

        # Load evaluate_deps via spec to avoid sys.modules collision with notify's index.
        spec = importlib.util.spec_from_file_location(
            'evaluate_deps_index',
            os.path.join(eval_deps_path, 'index.py'),
        )
        eval_deps = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eval_deps)
        result = eval_deps.handler(payload, None)

        assert result.get('is_ready') is True, (
            f"evaluate_deps returned is_ready=False with payload {payload}; "
            "assets_ready=true must reach evaluate_deps via the DDB record"
        )

    def test_add_to_assets_ready_set_stores_bool_and_set(self, monkeypatch):
        """add_to_assets_ready_set_and_get must write both assets_ready=true (for
        the notify_dependents SFN / Case B path) and assets_ready_set (for AND gate
        completeness check). If only ADD is used without SET assets_ready, OR-group
        tasks hang when assets arrive before task deps are satisfied."""
        from moto import mock_aws
        import boto3
        import dal as dal_module
        from dal import TokensRepo

        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal_module, '_dynamodb', None)

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            table = dynamodb.create_table(
                TableName='pipeline-tokens-moto',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            exec_name = 'task-2026-01-01-abc12345678'
            table.put_item(Item={'execution_name': exec_name})

            repo = TokensRepo('pipeline-tokens-moto')
            result = repo.add_to_assets_ready_set_and_get(exec_name, 'asset_a')

            assert result is not None
            assert result.get('assets_ready') is True, (
                "assets_ready=true must be stored so notify_dependents SFN can read it "
                "in the Case B path (asset arrives before task deps are satisfied)"
            )
            assert result.get('assets_ready_set') == {'asset_a'}


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
