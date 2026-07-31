"""
DAL for query_subscriptions Lambda (v0.79.3, ADR #75).

Single repo — reads asset-subscriptions table with pagination.
"""
from __future__ import annotations

import os
import boto3
from boto3.dynamodb.conditions import Key
from typing import List, Dict, Optional


_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb


class SubscriptionsRepo:
    """asset-subscriptions table: dependency_key + subscriber composite."""

    def __init__(self, table_name: Optional[str] = None):
        # NOTE: this Lambda reads the table_name from env at call time
        # (SUBSCRIPTIONS_TABLE), not at module-init — preserves the
        # existing behavior where missing env raises at handler runtime
        # rather than at cold-start.
        self._explicit_table_name = table_name

    @property
    def table_name(self) -> str:
        if self._explicit_table_name:
            return self._explicit_table_name
        return os.environ['SUBSCRIPTIONS_TABLE']

    @property
    def table(self):
        return _resource().Table(self.table_name)

    def list_for_dependency(
        self,
        dependency_key: str,
        *,
        hard_cap: int = 10000,
        consistent: bool = True,
    ) -> List[Dict]:
        """Paginated read of subscribers for a given dependency key."""
        subscribers: List[Dict] = []
        last_key = None
        while True:
            kwargs = {
                'KeyConditionExpression': Key('dependency_key').eq(dependency_key),
                'ConsistentRead': consistent,
            }
            if last_key:
                kwargs['ExclusiveStartKey'] = last_key
            response = self.table.query(**kwargs)
            subscribers.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
            if len(subscribers) >= hard_cap:
                # Caller logs.
                break
        return subscribers


subscriptions_repo = SubscriptionsRepo()
