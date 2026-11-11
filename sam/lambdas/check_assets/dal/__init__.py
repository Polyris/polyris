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
    """Subscriptions table — composite key dependency_key + subscriber.
    Writes `wait_for` subscriptions into DependencySubscriptionsTable (same
    table that holds task-to-task deps, keyed by dependency_key prefix:
    `asset:<name>` for wait_for, `<upstream>-<pipeline_exec_short>` for task
    deps). SUBSCRIPTIONS_TABLE env var is wired to that table in
    sam/template.yaml."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get(
            'SUBSCRIPTIONS_TABLE', 'polyris-dep-subscriptions'
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
    """pipeline-tokens table — keyed by execution_name.

    Two write paths:
    - mark_assets_ready: all wait_for assets were immediately satisfied at
      registration (no subscriptions needed). Writes the legacy bool AND the
      new set fields so evaluate_deps works on both old and new code paths.
    - write_pending_assets: called after saving subscriptions for assets not
      yet ready. Writes `wait_for_assets` = subscribed names so
      notify_asset_subscribers can check AND-completeness as each arrives.
    """

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get('TOKENS_TABLE', 'pipeline-tokens')

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def mark_assets_ready(self, execution_name: str, asset_names: List[str]) -> None:
        """Mark all wait_for assets as satisfied (immediately-ready path).
        Writes legacy `assets_ready=true` bool AND the new StringSet fields
        so evaluate_deps works on both code paths. Best-effort — caller logs
        and continues on failure."""
        name_set = set(asset_names)
        self.table.update_item(
            Key={'execution_name': execution_name},
            UpdateExpression='SET assets_ready = :true, wait_for_assets = :names, assets_ready_set = :names',
            ExpressionAttributeValues={
                ':true': True,
                ':names': name_set,
            },
            ConditionExpression='attribute_exists(execution_name)',
        )

    def write_pending_assets(self, execution_name: str, asset_names: List[str]) -> None:
        """Write `wait_for_assets` = subscribed asset names so
        notify_asset_subscribers can check AND completeness as each arrives.
        Best-effort — caller logs and continues on failure."""
        self.table.update_item(
            Key={'execution_name': execution_name},
            UpdateExpression='SET wait_for_assets = :names',
            ExpressionAttributeValues={':names': set(asset_names)},
            ConditionExpression='attribute_exists(execution_name)',
        )

    def add_assets_to_ready_set_and_get(self, execution_name: str, asset_names: List[str]) -> Optional[Dict]:
        """Atomically ADD a list of asset names to assets_ready_set, return the
        updated record. Takes a list (unlike notify_asset_subscribers' single-name
        variant). Does not swallow exceptions — caller handles errors."""
        response = self.table.update_item(
            Key={'execution_name': execution_name},
            UpdateExpression='ADD assets_ready_set :names',
            ExpressionAttributeValues={':names': set(asset_names)},
            ConditionExpression='attribute_exists(execution_name)',
            ReturnValues='ALL_NEW',
        )
        return response.get('Attributes')


asset_events_repo = AssetEventsRepo()
subscriptions_repo = SubscriptionsRepo()
tokens_repo = TokensRepo()
