"""
Tests for check_assets Lambda.

Tests AND/OR logic, freshness checking, and race condition handling.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
# pytest-mock: mocker fixture
from datetime import datetime, timezone, timedelta


# Mock boto3 before importing handler
@pytest.fixture(autouse=True)
def mock_dynamodb(mocker):
    """Mock DynamoDB for all tests."""
    # v0.79.3 (ADR #75): _dynamodb removed from index; DAL repo singletons.


def make_asset_event(asset_name: str, hours_ago: float = 0, uri: str = "s3://bucket/path"):
    """Create a mock asset event."""
    event_time = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        'asset_name': asset_name,
        'event_time': event_time,
        'uri': uri
    }


def make_consecutive_event(asset_name: str, execution_date: str, hours_ago: float = 0):
    """Create a mock asset event with execution_date for consecutive tests."""
    event_time = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    return {
        'asset_name': asset_name,
        'event_time': event_time,
        'execution_date': execution_date,
        'uri': f's3://bucket/{asset_name}/{execution_date}'
    }


class TestEvaluateWaitFor:
    """Test AND/OR logic evaluation."""
    
    def test_empty_wait_for_returns_ready(self, mocker):
        """Empty wait_for should return ready=True."""
        from check_assets.index import handler
        
        result = handler({'wait_for': []}, None)
        
        assert result['ready'] is True
        assert result['assets'] == []
    
    def test_single_asset_ready(self, mocker):
        """Single available asset should return ready=True."""
        from check_assets.index import handler
        
        # Mock DynamoDB table
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [make_asset_event('inventory', hours_ago=1)]
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{'asset_name': 'inventory', 'freshness_hours': None}],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is True
        assert len(result['assets']) == 1
        assert result['assets'][0]['name'] == 'inventory'
        assert result['assets'][0]['ready'] is True
    
    def test_single_asset_not_ready(self, mocker):
        """Missing asset should return ready=False and save subscription."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': []}  # No events
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{'asset_name': 'missing', 'freshness_hours': None}],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is False
        assert result['assets'][0]['ready'] is False
        assert result['assets'][0]['reason'] == 'no_event'
        
        # Verify subscription was saved
        mock_table.put_item.assert_called()
    
    def test_freshness_check_stale(self, mocker):
        """Asset older than freshness_hours should return ready=False."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {
            'Items': [make_asset_event('inventory', hours_ago=48)]  # 48 hours old
        }
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{'asset_name': 'inventory', 'freshness_hours': 24}],  # Require within 24h
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is False
        assert result['assets'][0]['reason'] == 'stale'
    
    def test_and_logic_all_ready(self, mocker):
        """AND: All assets ready -> ready=True."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [make_asset_event('a', hours_ago=1)]},
            {'Items': [make_asset_event('b', hours_ago=2)]},
        ]
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [
                    {'asset_name': 'a', 'freshness_hours': None},
                    {'asset_name': 'b', 'freshness_hours': None}
                ],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is True
    
    def test_and_logic_one_missing(self, mocker):
        """AND: One asset missing -> ready=False."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [make_asset_event('a', hours_ago=1)]},
            {'Items': []},  # b is missing
        ]
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [
                    {'asset_name': 'a', 'freshness_hours': None},
                    {'asset_name': 'b', 'freshness_hours': None}
                ],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is False
    
    def test_or_logic_one_ready(self, mocker):
        """OR: One asset ready -> ready=True."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.side_effect = [
            {'Items': [make_asset_event('a', hours_ago=1)]},
            {'Items': []},  # b is missing
        ]
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{
                    'operator': 'OR',
                    'assets': [
                        {'asset_name': 'a', 'freshness_hours': None},
                        {'asset_name': 'b', 'freshness_hours': None}
                    ]
                }],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is True
    
    def test_or_logic_all_missing(self, mocker):
        """OR: All assets missing -> ready=False."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': []}  # All missing
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{
                    'operator': 'OR',
                    'assets': [
                        {'asset_name': 'a', 'freshness_hours': None},
                        {'asset_name': 'b', 'freshness_hours': None}
                    ]
                }],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is False


class TestRaceCondition:
    """Test race condition handling."""
    
    def test_race_condition_resolved(self, mocker):
        """If asset appears after subscription, should return ready=True."""
        from check_assets.index import handler
        
        mock_table = mocker.MagicMock()
        
        # First check: no asset
        # After subscription: asset appears
        call_count = [0]
        def mock_query(**kwargs):
            call_count[0] += 1
            if call_count[0] <= 1:
                return {'Items': []}  # First check - missing
            else:
                return {'Items': [make_asset_event('inventory', hours_ago=0)]}  # Re-check - found
        
        mock_table.query.side_effect = mock_query
        
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{'asset_name': 'inventory', 'freshness_hours': None}],
                'task_name': 'test_task',
                'wait_token': 'token123',
                'ttl': 9999999
            }, None)
        
        assert result['ready'] is True
        # Subscription should be saved then deleted
        mock_table.put_item.assert_called()
        mock_table.delete_item.assert_called()


class TestPartialRecheck:
    """Asset a arrives before its subscription is saved; b may arrive later.

    Two sub-scenarios:
    - Gate incomplete after credit (b still missing) → ready=False, b's sub stays
    - Gate complete after credit (b also arrived concurrently) → ready=True, all subs deleted

    The second scenario is the one the previous review missed: notify(b) adds b
    and sees {b} ⊄ {a, b}, doesn't wake; check_assets credits a, sees {a, b} ⊇
    {a, b} — the completeness check here is what prevents the hung task.
    """

    def _make_partial_handler_mocks(self, mocker, credit_return: dict):
        """Set up handler mocks for partial-recheck tests.

        First two queries return empty (a and b missing).
        Re-check: a is ready, b is still missing.
        add_to_assets_ready_set_and_get returns credit_return.
        """
        from check_assets.index import asset_events_repo, subscriptions_repo, tokens_repo

        call_count = [0]

        def mock_query(**kwargs):
            call_count[0] += 1
            asset = kwargs.get('ExpressionAttributeValues', {}).get(':name', '')
            if call_count[0] <= 2:
                return {'Items': []}
            elif asset == 'a':
                return {'Items': [make_asset_event('a', hours_ago=0)]}
            else:
                return {'Items': []}

        mock_events_table = mocker.MagicMock()
        mock_events_table.query.side_effect = mock_query
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)

        mocker.patch.object(tokens_repo, 'add_assets_to_ready_set_and_get',
                            return_value=credit_return)
        deleted = []
        mocker.patch.object(subscriptions_repo, 'delete',
                            side_effect=lambda asset, sub: deleted.append(asset))
        mocker.patch.object(tokens_repo, 'write_pending_assets')
        return deleted

    def test_partial_recheck_incomplete_gate(self, mocker):
        """a credited; gate still incomplete ({a} ⊄ {a, b}) → ready=False, b sub stays."""
        from check_assets.index import handler
        deleted = self._make_partial_handler_mocks(
            mocker,
            credit_return={'assets_ready_set': {'a'}, 'wait_for_assets': {'a', 'b'}},
        )
        result = handler({
            'wait_for': [
                {'asset_name': 'a', 'freshness_hours': None},
                {'asset_name': 'b', 'freshness_hours': None},
            ],
            'task_name': 'consumer', 'wait_token': 'tok',
            'execution_name': 'consumer-2026-01-01-abc12345678', 'ttl': 9999999,
        }, None)
        assert result['ready'] is False
        assert 'a' in deleted, "a's subscription must be deleted after successful credit"
        assert 'b' not in deleted, "b's subscription must stay (still waiting)"

    def test_partial_recheck_complete_gate(self, mocker):
        """a credited; notify added b concurrently → gate complete ({a,b} ⊇ {a,b})
        → ready=True, all subscriptions deleted. Without this check the task hangs:
        notify saw incomplete set and didn't wake; nobody else will."""
        from check_assets.index import handler
        deleted = self._make_partial_handler_mocks(
            mocker,
            credit_return={'assets_ready_set': {'a', 'b'}, 'wait_for_assets': {'a', 'b'}},
        )
        result = handler({
            'wait_for': [
                {'asset_name': 'a', 'freshness_hours': None},
                {'asset_name': 'b', 'freshness_hours': None},
            ],
            'task_name': 'consumer', 'wait_token': 'tok',
            'execution_name': 'consumer-2026-01-01-abc12345678', 'ttl': 9999999,
        }, None)
        assert result['ready'] is True
        assert 'a' in deleted, "a's subscription must be deleted"
        assert 'b' in deleted, "b's subscription must be deleted (gate complete)"


class TestGateWriteOrdering:
    """Gate record must be written before subscriptions (AND-gate race prevention)."""

    def test_gate_written_before_first_subscription(self, mocker):
        """write_pending_assets must precede every put_asset_subscription call.

        If a subscription is saved first and an asset arrives before the gate is
        written, notify_asset_subscribers sees no wait_for_assets field and wakes
        the task immediately (AND degrades to OR).
        """
        from check_assets.index import handler, asset_events_repo, subscriptions_repo, tokens_repo

        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {'Items': []}  # both assets missing
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)

        call_order = []
        mocker.patch.object(tokens_repo, 'write_pending_assets',
                            side_effect=lambda *a, **kw: call_order.append('gate'))
        mocker.patch.object(subscriptions_repo, 'put_asset_subscription',
                            side_effect=lambda *a, **kw: call_order.append('subscription'))

        handler({
            'wait_for': [
                {'asset_name': 'a', 'freshness_hours': None},
                {'asset_name': 'b', 'freshness_hours': None},
            ],
            'task_name': 'consumer',
            'wait_token': 'tok',
            'execution_name': 'consumer-2026-01-01-abc12345678',
            'ttl': 9999999,
        }, None)

        assert call_order[0] == 'gate', f"gate must precede subscriptions; got order: {call_order}"
        assert call_order.count('subscription') == 2


class TestHelperFunctions:
    """Test helper functions."""

    def test_is_fresh_within_limit(self, mocker):
        """Asset within freshness limit should return True."""
        from check_assets.index import _is_fresh
        
        event_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        assert _is_fresh(event_time, 24) is True
    
    def test_is_fresh_beyond_limit(self, mocker):
        """Asset beyond freshness limit should return False."""
        from check_assets.index import _is_fresh
        
        event_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        assert _is_fresh(event_time, 24) is False
    
    def test_is_fresh_handles_z_suffix(self, mocker):
        """Should handle Z timezone suffix."""
        from check_assets.index import _is_fresh
        
        event_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%SZ')
        assert _is_fresh(event_time, 24) is True
    
    def test_get_missing_assets(self, mocker):
        """Should extract only non-ready assets."""
        from check_assets.index import _get_missing_assets
        
        results = [
            {'name': 'a', 'ready': True},
            {'name': 'b', 'ready': False, 'freshness_hours': 24},
            {'name': 'c', 'ready': True},
            {'name': 'd', 'ready': False},
        ]
        
        missing = _get_missing_assets(results)
        
        assert len(missing) == 2
        assert missing[0]['name'] == 'b'
        assert missing[0]['freshness_hours'] == 24
        assert missing[1]['name'] == 'd'


class TestConsecutiveCheck:
    """Test consecutive days asset checking."""
    
    def test_consecutive_all_7_days_present(self, mocker):
        """7/7 consecutive days present should return ready=True."""
        from check_assets.index import _check_asset_consecutive
        
        # Create events for 7 consecutive days ending 2026-02-22
        items = [
            make_consecutive_event('daily', f'2026-02-{16+i:02d}', hours_ago=i*24)
            for i in range(7)
        ]
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': items}
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _check_asset_consecutive('daily', 7, '2026-02-22')
        
        assert result['ready'] is True
        assert result['consecutive_days'] == 7
        assert len(result['found_dates']) == 7
    
    def test_consecutive_missing_wednesday(self, mocker):
        """6/7 days (missing Wednesday) should return ready=False."""
        from check_assets.index import _check_asset_consecutive
        
        # Create events for 6 days, skip Feb 19 (Wednesday)
        dates = ['2026-02-16', '2026-02-17', '2026-02-18',
                 '2026-02-20', '2026-02-21', '2026-02-22']
        items = [make_consecutive_event('daily', d) for d in dates]
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': items}
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _check_asset_consecutive('daily', 7, '2026-02-22')
        
        assert result['ready'] is False
        assert '2026-02-19' in result['missing_dates']
        assert result['reason'] == 'consecutive_incomplete'
    
    def test_consecutive_no_events(self, mocker):
        """0 events should return ready=False."""
        from check_assets.index import _check_asset_consecutive
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': []}
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _check_asset_consecutive('daily', 7, '2026-02-22')
        
        assert result['ready'] is False
        assert len(result['missing_dates']) == 7
    
    def test_consecutive_duplicate_events_same_date(self, mocker):
        """8 events with 7 unique dates (1 duplicate) should return ready=True."""
        from check_assets.index import _check_asset_consecutive
        
        dates = ['2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19',
                 '2026-02-20', '2026-02-21', '2026-02-22',
                 '2026-02-20']  # duplicate
        items = [make_consecutive_event('daily', d) for d in dates]
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': items}
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _check_asset_consecutive('daily', 7, '2026-02-22')
        
        assert result['ready'] is True
        assert len(result['found_dates']) == 7
    
    def test_consecutive_reference_date_calculation(self, mocker):
        """Should check correct date range based on reference_date."""
        from check_assets.index import _check_asset_consecutive
        
        # 3 consecutive days ending 2026-01-15 = [01-13, 01-14, 01-15]
        items = [
            make_consecutive_event('daily', '2026-01-13'),
            make_consecutive_event('daily', '2026-01-14'),
            make_consecutive_event('daily', '2026-01-15'),
        ]
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': items}
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = _check_asset_consecutive('daily', 3, '2026-01-15')
        
        assert result['ready'] is True
        assert result['found_dates'] == ['2026-01-13', '2026-01-14', '2026-01-15']
    
    def test_consecutive_in_handler_with_subscription(self, mocker):
        """Handler should save consecutive subscription when not ready."""
        from check_assets.index import handler
        
        # Only 3/7 days present
        items = [
            make_consecutive_event('daily', '2026-02-20'),
            make_consecutive_event('daily', '2026-02-21'),
            make_consecutive_event('daily', '2026-02-22'),
        ]
        
        mock_table = mocker.MagicMock()
        mock_table.query.return_value = {'Items': items}
        mock_table.put_item = mocker.MagicMock()
        mock_dynamodb = mocker.MagicMock()
        mock_dynamodb.Table.return_value = mock_table
        
        # v0.79.3 (ADR #75) — DAL repos. Wire mock_table into both
        # repos so DAL calls hit the same mock the test set up.
        from check_assets.index import asset_events_repo, subscriptions_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_table)
        result = handler({
                'wait_for': [{'asset_name': 'daily', 'consecutive_days': 7}],
                'task_name': 'weekly_task',
                'wait_token': 'token123',
                'execution_name': 'weekly-2026-02-22-abc',
                'ttl': 9999999,
                'current_date': '2026-02-22'
            }, None)
        
        assert result['ready'] is False
        
        # Verify subscription was saved with consecutive params
        put_calls = mock_table.put_item.call_args_list
        assert len(put_calls) >= 1
        saved_item = put_calls[0][1]['Item']
        assert saved_item['subscription_type'] == 'asset_consecutive'
        assert saved_item['consecutive_days'] == 7
        assert saved_item['reference_date'] == '2026-02-22'


class TestAssetsReadyFlag:
    """Case B (Option J): check_assets must flip assets_ready=true on the
    subscriber's task record when returning ready — otherwise a task with
    wait_for + still-pending task_deps hangs forever: evaluate_deps (called
    later when task_deps complete) would see wait_for + assets_ready=false
    on the record and return verdict='wait', but no async asset arrival will
    ever trigger notify_asset_subscribers to flip the flag (there is no
    subscription — assets were already ready at registration)."""

    def test_writes_assets_ready_when_immediately_ready(self, mocker):
        """The critical path: assets were ready at first check → flag written."""
        from index import handler

        # asset already exists → ready immediately
        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {
            'Items': [{'asset_name': 'inventory', 'event_time': '2026-07-27T10:00:00Z'}]
        }
        from index import asset_events_repo, tokens_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)

        mark_mock = mocker.patch.object(tokens_repo, 'mark_assets_ready')

        result = handler({
            'wait_for': [{'asset_name': 'inventory'}],
            'task_name': 'consumer',
            'wait_token': 'AQC...',
            'execution_name': 'consumer-2026-07-27-abc',
            'ttl': 0,
            'current_date': '2026-07-27',
        }, None)

        assert result['ready'] is True
        mark_mock.assert_called_once_with('consumer-2026-07-27-abc', ['inventory'])

    def test_does_not_write_flag_when_not_ready(self, mocker):
        """Assets not ready → no flag write (notify_asset_subscribers will
        write it when the asset actually arrives)."""
        from index import handler

        # asset does NOT exist → not ready
        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {'Items': []}
        mock_sub_table = mocker.MagicMock()
        from index import asset_events_repo, subscriptions_repo, tokens_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)

        mark_mock = mocker.patch.object(tokens_repo, 'mark_assets_ready')
        write_mock = mocker.patch.object(tokens_repo, 'write_pending_assets')

        result = handler({
            'wait_for': [{'asset_name': 'inventory'}],
            'task_name': 'consumer',
            'wait_token': 'AQC...',
            'execution_name': 'consumer-2026-07-27-abc',
            'ttl': 0,
            'current_date': '2026-07-27',
        }, None)

        assert result['ready'] is False
        mark_mock.assert_not_called()
        write_mock.assert_called_once_with('consumer-2026-07-27-abc', ['inventory'])

    def test_flag_write_failure_does_not_break_handler(self, mocker):
        """A DDB failure on mark_assets_ready must not crash check_assets —
        the immediate registration signal path still functions; the flag is
        only needed for the later notify_dependents re-check. Log-and-continue
        is the correct behaviour (matches subscriptions_repo pattern)."""
        from index import handler

        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {
            'Items': [{'asset_name': 'inventory', 'event_time': '2026-07-27T10:00:00Z'}]
        }
        from index import asset_events_repo, tokens_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)

        mocker.patch.object(tokens_repo, 'mark_assets_ready',
                            side_effect=Exception('DDB throttled'))

        # Must not raise
        result = handler({
            'wait_for': [{'asset_name': 'inventory'}],
            'task_name': 'consumer',
            'wait_token': 'AQC...',
            'execution_name': 'consumer-2026-07-27-abc',
            'ttl': 0,
            'current_date': '2026-07-27',
        }, None)

        assert result['ready'] is True  # ready reflects asset availability, not flag write


class TestHasOrGroup:
    """_has_or_group must detect OR semantics at any nesting depth."""

    def test_flat_and_list_returns_false(self):
        from index import _has_or_group
        assert _has_or_group([{'asset_name': 'a'}, {'asset_name': 'b'}]) is False

    def test_top_level_or_group_returns_true(self):
        from index import _has_or_group
        assert _has_or_group([
            {'operator': 'OR', 'assets': [{'asset_name': 'a'}, {'asset_name': 'b'}]}
        ]) is True

    def test_top_level_and_group_returns_false(self):
        from index import _has_or_group
        assert _has_or_group([
            {'operator': 'AND', 'assets': [{'asset_name': 'a'}, {'asset_name': 'b'}]}
        ]) is False

    def test_nested_or_inside_and_returns_true(self):
        from index import _has_or_group
        assert _has_or_group([
            {'operator': 'AND', 'assets': [
                {'asset_name': 'a'},
                {'operator': 'OR', 'assets': [{'asset_name': 'b'}, {'asset_name': 'c'}]}
            ]}
        ]) is True

    def test_empty_list_returns_false(self):
        from index import _has_or_group
        assert _has_or_group([]) is False


class TestOrGroupAndGate:
    """AND-gate must be skipped for OR-group wait_for specs.

    wait_for=[a | b] should fire when ANY one arrives — the AND gate would
    incorrectly require both.  Regression guard for the bug found in
    break-review: notify_asset_subscribers' AND check
    (wait_for_assets ⊆ assets_ready_set) used all subscribed names as
    wait_for_assets regardless of operator."""

    def test_write_pending_not_called_for_or_group(self, mocker):
        """OR group → _write_pending_assets skipped → no wait_for_assets on record
        → notify_asset_subscribers falls back to legacy first-arrival behavior."""
        from index import handler

        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {'Items': []}  # neither asset ready
        mock_sub_table = mocker.MagicMock()
        from index import asset_events_repo, subscriptions_repo, tokens_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)

        mark_mock = mocker.patch.object(tokens_repo, 'mark_assets_ready')
        write_mock = mocker.patch.object(tokens_repo, 'write_pending_assets')

        result = handler({
            'wait_for': [{'operator': 'OR', 'assets': [
                {'asset_name': 'daily'},
                {'asset_name': 'manual_override'},
            ]}],
            'task_name': 'consumer',
            'wait_token': 'AQC...',
            'execution_name': 'consumer-2026-07-27-abc',
            'ttl': 0,
            'current_date': '2026-07-27',
        }, None)

        assert result['ready'] is False
        mark_mock.assert_not_called()
        # AND gate must NOT be armed for OR groups
        write_mock.assert_not_called()

    def test_write_pending_called_for_and_list(self, mocker):
        """Plain AND list → _write_pending_assets IS called (AND gate armed)."""
        from index import handler

        mock_events_table = mocker.MagicMock()
        mock_events_table.query.return_value = {'Items': []}
        mock_sub_table = mocker.MagicMock()
        from index import asset_events_repo, subscriptions_repo, tokens_repo
        mocker.patch.object(type(asset_events_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_events_table)
        mocker.patch.object(type(subscriptions_repo), 'table',
                            new_callable=mocker.PropertyMock,
                            return_value=mock_sub_table)

        mocker.patch.object(tokens_repo, 'mark_assets_ready')
        write_mock = mocker.patch.object(tokens_repo, 'write_pending_assets')

        handler({
            'wait_for': [{'asset_name': 'a'}, {'asset_name': 'b'}],
            'task_name': 'consumer',
            'wait_token': 'AQC...',
            'execution_name': 'consumer-2026-07-27-abc',
            'ttl': 0,
            'current_date': '2026-07-27',
        }, None)

        write_mock.assert_called_once_with('consumer-2026-07-27-abc', ['a', 'b'])


class TestDynamoDBExpressions:
    """Verify DDB expressions with moto — actual ADD semantics, not mocked methods.

    Principle #14: mock at the external boundary (boto3 via moto), never at
    internal logic. These tests catch expression bugs (wrong attribute names,
    wrong set type) that mocker-based tests cannot see.
    """

    def test_add_accumulates_across_calls(self, monkeypatch):
        """Two ADD calls on the same row build up the set incrementally,
        matching the notify(b) + credit(a) sequence from the race scenario."""
        from moto import mock_aws
        import boto3
        import check_assets.dal as dal_module
        from check_assets.dal import TokensRepo

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
            table.put_item(Item={
                'execution_name': exec_name,
                'wait_for_assets': {'a', 'b'},
            })

            repo = TokensRepo('pipeline-tokens-moto')

            # Simulate notify_asset_subscribers adding b first
            result_b = repo.add_assets_to_ready_set_and_get(exec_name, ['b'])
            assert result_b is not None
            assert result_b['assets_ready_set'] == {'b'}
            gate = result_b.get('wait_for_assets', set())
            assert not (result_b['assets_ready_set'] >= gate), "gate must not be complete with only b"

            # Simulate check_assets crediting a — gate completes
            result_a = repo.add_assets_to_ready_set_and_get(exec_name, ['a'])
            assert result_a is not None
            assert result_a['assets_ready_set'] == {'a', 'b'}
            gate2 = result_a.get('wait_for_assets', set())
            assert result_a['assets_ready_set'] >= gate2, "gate must be complete after both assets credited"


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
