"""
Data Access Layer: Task Events Table.

Encapsulates all DynamoDB operations for the task-events table
(TASK_EVENTS_TABLE).

Table schema:
    PK: task_run_id, SK: event_time.
    GSI: execution-name-index (execution_name).
"""

from boto3.dynamodb.conditions import Key
from config import dynamodb, TASK_EVENTS_TABLE


class TaskEventsRepo:
    """Repository for task-events table."""

    def __init__(self):
        self._table_name = TASK_EVENTS_TABLE

    @property
    def table(self):
        return dynamodb.Table(self._table_name)

    def query_by_task_run_id(self, task_run_id: str, ascending: bool = True) -> list:
        """Query events by task_run_id (PK)."""
        response = self.table.query(
            KeyConditionExpression=Key('task_run_id').eq(task_run_id),
            ScanIndexForward=ascending
        )
        return response.get('Items', [])

    def query_by_execution_name(self, execution_name: str, ascending: bool = True) -> list:
        """Query events by execution_name GSI."""
        response = self.table.query(
            IndexName='execution-name-index',
            KeyConditionExpression=Key('execution_name').eq(execution_name),
            ScanIndexForward=ascending
        )
        return response.get('Items', [])

    def put(self, item: dict) -> None:
        """Insert/overwrite a task event item.

        Required keys: task_run_id (PK), event_time (SK).
        Callers should supply deterministic sort keys for retry safety
        (Principle #3 — idempotency).
        """
        self.table.put_item(Item=item)


# Singleton instance
task_events_repo = TaskEventsRepo()
