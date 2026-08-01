"""
Data Access Layer: Subscription Tables.

Encapsulates DynamoDB operations for dep-subscriptions (SUBSCRIPTIONS_TABLE).

Used by routes: executions, backfill.
"""

from config import dynamodb, SUBSCRIPTIONS_TABLE


class DepSubscriptionsRepo:
    """Repository for dep-subscriptions table.
    
    Table schema: PK: dependency_key, SK: subscriber.
    """

    def __init__(self):
        self._table_name = SUBSCRIPTIONS_TABLE

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    def delete(self, dependency_key: str, subscriber: str) -> None:
        self.table.delete_item(Key={
            'dependency_key': dependency_key,
            'subscriber': subscriber
        })


# Singleton instance
dep_subscriptions_repo = DepSubscriptionsRepo()
