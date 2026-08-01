"""
Data Access Layer: Asset Tables.

Encapsulates all DynamoDB operations for asset-related tables:
- asset-events (ASSET_EVENTS_TABLE)
- queued-asset-events (QUEUED_EVENTS_TABLE)

Used by routes: assets, backfill.
"""

from boto3.dynamodb.conditions import Key
from config import dynamodb, ASSET_EVENTS_TABLE, QUEUED_EVENTS_TABLE
from constants import Limits
from utils import scan_all, query_all


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
