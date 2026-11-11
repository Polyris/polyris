"""
DAL for notify_asset_subscribers Lambda (v0.79.3, ADR #75).

Two repositories — one per table this Lambda touches:
  - SubscriptionsRepo (read + delete on asset-subscriptions table)
  - AssetEventsRepo (read on asset-events table)

Both use the same `boto3.resource('dynamodb')` instance under the hood,
created lazily by a shared `_resource()` helper.
"""
from __future__ import annotations

import os
import boto3
from typing import List, Dict, Optional


_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb


class SubscriptionsRepo:
    """Subscriptions table: dependency_key + subscriber composite.
    Reads `wait_for` subscriptions from DependencySubscriptionsTable (same
    table that holds task-to-task deps, keyed by dependency_key prefix
    `asset:<name>` here). SUBSCRIPTIONS_TABLE env var is wired to that table
    in sam/template.yaml."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get(
            'SUBSCRIPTIONS_TABLE', 'polyris-dep-subscriptions'
        )

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def list_for_asset(self, asset_name: str, *, hard_cap: int = 10000) -> List[Dict]:
        """Return all subscribers for `asset:{asset_name}` (paginated)."""
        dependency_key = f'asset:{asset_name}'
        subscribers: List[Dict] = []
        last_key = None
        while True:
            kwargs = {
                'KeyConditionExpression': 'dependency_key = :dk',
                'ExpressionAttributeValues': {':dk': dependency_key},
            }
            if last_key:
                kwargs['ExclusiveStartKey'] = last_key
            response = self.table.query(**kwargs)
            subscribers.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            if len(subscribers) >= hard_cap:
                # Caller should log; we just stop pagination.
                break
        return subscribers

    def delete(self, asset_name: str, subscriber: str) -> None:
        """Delete a subscription after notify."""
        self.table.delete_item(Key={
            'dependency_key': f'asset:{asset_name}',
            'subscriber': subscriber,
        })


class AssetEventsRepo:
    """asset-events table: keyed by asset_name (PK)."""

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


class TokensRepo:
    """pipeline-tokens table — keyed by execution_name. Used to (a) flip
    assets_ready=true when a subscribed asset arrives so evaluate_deps sees a
    coherent view, and (b) read the subscriber's task record (dependencies,
    trigger_rule, wait_for) to hand to evaluate_deps."""

    def __init__(self, table_name: Optional[str] = None):
        self._table_name = table_name or os.environ.get('TOKENS_TABLE', 'pipeline-tokens')

    @property
    def table(self):
        return _resource().Table(self._table_name)

    def write_warn(
        self,
        execution_name: str,
        task_name: str,
        pipeline_execution: str,
        pipeline_name: str,
        date: str,
        error: str,
    ) -> None:
        """Write a _notify_warn_ record visible in the UI bell. Best-effort."""
        from datetime import datetime, timezone as tz
        now = datetime.now(tz.utc)
        self.table.put_item(Item={
            'execution_name': f'_notify_warn_{execution_name}',
            'task_name': task_name,
            'pipeline_execution': pipeline_execution,
            'pipeline_name': pipeline_name,
            'date': date,
            'status': 'failed',
            'error': error,
            'finished_at': now.isoformat(),
            'ttl': int(now.timestamp()) + 86400,
        })

    def add_to_assets_ready_set_and_get(self, execution_name: str, asset_name: str) -> Optional[Dict]:
        """Atomically ADD asset_name to `assets_ready_set` and SET assets_ready=true
        on the task record, return the full updated record. Setting assets_ready=true
        is required for the Case B path: when task deps are not yet satisfied,
        notify_dependents SFN later reads the DDB record and passes assets_ready to
        evaluate_deps — if the bool is absent, OR-group tasks hang permanently."""
        try:
            response = self.table.update_item(
                Key={'execution_name': execution_name},
                UpdateExpression='SET assets_ready = :true ADD assets_ready_set :name_set',
                ExpressionAttributeValues={':name_set': {asset_name}, ':true': True},
                ConditionExpression='attribute_exists(execution_name)',
                ReturnValues='ALL_NEW',
            )
            return response.get('Attributes')
        except Exception:
            return None


# Module singletons
subscriptions_repo = SubscriptionsRepo()
asset_events_repo = AssetEventsRepo()
tokens_repo = TokensRepo()
