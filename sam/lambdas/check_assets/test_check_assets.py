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
        mark_mock.assert_called_once_with('consumer-2026-07-27-abc')

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


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
