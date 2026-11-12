"""
Data Access Layer: Asset Tables.

Encapsulates all DynamoDB operations for asset-related tables:
- asset-events (ASSET_EVENTS_TABLE)
- queued-asset-events (QUEUED_EVENTS_TABLE)

Used by routes: assets, backfill.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from config import dynamodb, ASSET_EVENTS_TABLE, QUEUED_EVENTS_TABLE
from constants import Limits
from coordination_core.partitions import detect_granularity
from coordination_core.records import asset_fact_item
from utils import scan_all, query_all

#: A fact is keyed by (asset_name, event_time): retry a collision with a later millisecond.
_FACT_WRITE_ATTEMPTS = 5


class AssetEventsRepo:
    """Repository for asset-events table.
    
    Table schema: PK: asset_name, SK: event_time. GSI: date-index (execution_date).
    """

    def __init__(self):
        self._table_name = ASSET_EVENTS_TABLE

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    def put(self, item: dict) -> None:
        self.table.put_item(Item=item)

    def write_fact(self, *, asset_name: str, partition_key: str, granularity: Optional[str], cascade: str,
                   source_dag: str, source_task: str, uri: str, ttl_days: int,
                   now: Optional[datetime] = None) -> dict:
        """Record one asset fact (ADR #130 §1); returns the written item.

        Every fact writer outside run_task goes through here, so each fact has
        the shape asset conditions and the partition index read. A fact's
        partition is the partition of the run that produced it.
        ``granularity`` is the outlet's declared one; an outlet deployed
        before it was recorded has none, and the key's own format decides.
        """
        moment = now or datetime.now(timezone.utc)
        for _ in range(_FACT_WRITE_ATTEMPTS):
            item = asset_fact_item(
                asset_name=asset_name,
                partition_key=partition_key,
                granularity=granularity or detect_granularity(partition_key),
                cascade=cascade,
                source_dag=source_dag,
                source_task=source_task,
                uri=uri,
                now=moment,
                ttl_days=ttl_days,
            )
            try:
                # Never overwrite another fact of this asset written in the same millisecond.
                self.table.put_item(Item=item, ConditionExpression='attribute_not_exists(event_time)')
                return item
            except ClientError as e:
                if e.response.get('Error', {}).get('Code') != 'ConditionalCheckFailedException':
                    raise
                moment += timedelta(milliseconds=1)
        raise RuntimeError(f"no free event_time for a fact of {asset_name} after {_FACT_WRITE_ATTEMPTS} tries")

    def query_by_asset(self, asset_name: str, limit: int = 50,
                       descending: bool = True) -> list:
        response = self.table.query(
            KeyConditionExpression=Key('asset_name').eq(asset_name),
            ScanIndexForward=not descending,
            Limit=limit
        )
        return response.get('Items', [])

    def query_by_date(self, date: str, limit: int = 20,
                      descending: bool = True) -> list:
        response = self.table.query(
            IndexName='date-index',
            KeyConditionExpression=Key('execution_date').eq(date),
            ScanIndexForward=not descending,
            Limit=limit
        )
        return response.get('Items', [])

    def delete_by_asset(self, asset_name: str) -> int:
        """Delete all events for an asset. Returns count of deleted items.

        Paginates via query_all up to Limits.MAX_SCAN_ITEMS so callers can
        safely use this for full asset cleanup without silent truncation.
        """
        items = query_all(
            self.table,
            max_items=Limits.MAX_SCAN_ITEMS,
            KeyConditionExpression=Key('asset_name').eq(asset_name),
            ScanIndexForward=True,
        )
        with self.table.batch_writer() as batch:
            for item in items:
                batch.delete_item(Key={
                    'asset_name': item['asset_name'],
                    'event_time': item['event_time'],
                })
        return len(items)

    def list_asset_names(self, max_items: int = None) -> list:
        """Get unique asset names that have events.

        Uses paginated scan with ProjectionExpression='asset_name' to keep
        per-page payload small. Iterates the full table so orphan-detection
        callers see every asset, not just the first page.

        Args:
            max_items: Safety cap on scanned rows (defaults to Limits.MAX_SCAN_ITEMS).
                       If hit, scan_all logs a warning so callers know the result
                       may be incomplete.
        """
        if max_items is None:
            max_items = Limits.MAX_SCAN_ITEMS
        items = scan_all(
            self.table,
            max_items=max_items,
            ProjectionExpression='asset_name',
        )
        names = {item.get('asset_name', '') for item in items}
        names.discard('')
        return list(names)


class QueuedEventsRepo:
    """Repository for queued-asset-events table.
    
    Table schema: PK: dag_date, SK: asset_name.
    """

    def __init__(self):
        self._table_name = QUEUED_EVENTS_TABLE

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    def put(self, item: dict) -> None:
        self.table.put_item(Item=item)

    def query_by_dag_date(self, dag_date: str) -> list:
        response = self.table.query(
            KeyConditionExpression=Key('dag_date').eq(dag_date)
        )
        return response.get('Items', [])

    def delete(self, dag_date: str, asset_name: str) -> None:
        self.table.delete_item(Key={
            'dag_date': dag_date,
            'asset_name': asset_name
        })


# Singleton instances
asset_events_repo = AssetEventsRepo()
queued_events_repo = QueuedEventsRepo()
