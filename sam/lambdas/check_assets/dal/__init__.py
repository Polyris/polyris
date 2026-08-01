"""
DAL for check_assets Lambda (v0.79.3, ADR #75).

Reads from asset-events and writes to asset-subscriptions.
Pattern mirrors notify_asset_subscribers/dal/__init__.py.
"""
from __future__ import annotations

import os
import boto3
from typing import Optional, Dict, List


_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb


class AssetEventsRepo:
    """asset-events table — keyed by asset_name."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get(
            'ASSET_EVENTS_TABLE', 'asset-events'
        )

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def query_recent(self, asset_name: str, *, limit: int = 30) -> List[Dict]:
        """Return recent events for an asset (newest first)."""
        response = self.table.query(
            KeyConditionExpression='asset_name = :name',
            ExpressionAttributeValues={':name': asset_name},
            ScanIndexForward=False,
            Limit=limit,
        )
        return response.get('Items', [])

    def get_latest(self, asset_name: str) -> Optional[Dict]:
        """Get the most-recent event for an asset, or None."""
        items = self.query_recent(asset_name, limit=1)
        return items[0] if items else None


class SubscriptionsRepo:
    """asset-subscriptions table — composite key dependency_key + subscriber."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get(
            'SUBSCRIPTIONS_TABLE', 'asset-subscriptions'
        )

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def put_asset_subscription(
        self,
        asset_name: str,
        subscriber: str,
        wait_token: str,
        execution_name: str,
        ttl: int,
        *,
        freshness_hours: Optional[int] = None,
        consecutive_days: Optional[int] = None,
        reference_date: Optional[str] = None,
    ) -> None:
        """Write a single asset subscription. Best-effort — exceptions
        propagate to the caller (check_assets logs and continues)."""
        item: Dict = {
            'dependency_key': f'asset:{asset_name}',
            'subscriber': subscriber,
            'wait_token': wait_token,
            'execution_name': execution_name,
            'subscription_type': 'asset',
            'ttl': ttl,
        }
        if freshness_hours is not None:
            item['freshness_hours'] = freshness_hours
        if consecutive_days is not None:
            item['subscription_type'] = 'asset_consecutive'
            item['consecutive_days'] = consecutive_days
            item['reference_date'] = reference_date
        self.table.put_item(Item=item)

    def delete(self, asset_name: str, subscriber: str) -> None:
        """Remove a subscription."""
        self.table.delete_item(Key={
            'dependency_key': f'asset:{asset_name}',
            'subscriber': subscriber,
        })


class TokensRepo:
    """pipeline-tokens table — keyed by execution_name. Used only to mark
    assets_ready=true so evaluate_deps (called later by notify_dependents when
    task_deps complete) can tell whether wait_for assets were already ready at
    registration time."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get('TOKENS_TABLE', 'pipeline-tokens')

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def mark_assets_ready(self, execution_name: str) -> None:
        """Idempotent SET assets_ready=true on the task record. Best-effort —
        the caller (check_assets) logs and continues on failure."""
        self.table.update_item(
            Key={'execution_name': execution_name},
            UpdateExpression='SET assets_ready = :true',
            ExpressionAttributeValues={':true': True},
            ConditionExpression='attribute_exists(execution_name)',
        )


asset_events_repo = AssetEventsRepo()
subscriptions_repo = SubscriptionsRepo()
tokens_repo = TokensRepo()
