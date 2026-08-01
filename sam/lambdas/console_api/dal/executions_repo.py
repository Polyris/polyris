"""
Data Access Layer: Pipeline Executions Table.

Encapsulates all DynamoDB operations for the pipeline-executions table
(TABLE_NAME / pipeline_tokens). Used by routes: executions, tasks, slack,
pipelines, health, notifications, backfill.

Table schema:
    PK: execution_name (str)
    GSIs: pipeline-execution-index (pipeline_execution), date-pipeline-index (date, pipeline_name)
"""

from boto3.dynamodb.conditions import Key
from config import dynamodb, TABLE_NAME
from utils import scan_all, query_all


class ExecutionsRepo:
    """Repository for pipeline-executions (tokens) table."""

    def __init__(self):
        self._table_name = TABLE_NAME

    @property
    def table(self):
        """Lazy table reference (new on every access for Lambda reuse safety)."""
        return dynamodb.Table(self._table_name)

    # ── Single-item operations ────────────────────────────────────────────

    def get(self, execution_name: str, consistent: bool = False) -> dict | None:
        """Get a single execution by execution_name."""
        kwargs = {'Key': {'execution_name': execution_name}}
        if consistent:
            kwargs['ConsistentRead'] = True
        response = self.table.get_item(**kwargs)
        return response.get('Item')

    def put(self, item: dict) -> None:
        """Put an execution item."""
        self.table.put_item(Item=item)

    def delete(self, execution_name: str) -> None:
        """Delete an execution by execution_name."""
        self.table.delete_item(Key={'execution_name': execution_name})

    def update(self, execution_name: str, update_expr: str,
               expr_values: dict = None, expr_names: dict = None,
               condition_expr: str = None) -> dict:
        """Generic update_item with optional condition expression.
        
        Returns the boto3 response dict.
        Raises ClientError on ConditionalCheckFailedException etc.
        """
        params = {
            'Key': {'execution_name': execution_name},
            'UpdateExpression': update_expr,
        }
        if expr_values:
            params['ExpressionAttributeValues'] = expr_values
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        if condition_expr:
            params['ConditionExpression'] = condition_expr
        return self.table.update_item(**params)

    # ── GSI: pipeline-execution-index ─────────────────────────────────────

    def query_by_pipeline_execution(self, pipeline_execution: str,
                                     projection: str = None,
                                     expr_names: dict = None,
                                     limit: int = None) -> list:
        """Query pipeline-execution-index GSI. Returns all pages."""
        params = {
            'IndexName': 'pipeline-execution-index',
            'KeyConditionExpression': Key('pipeline_execution').eq(pipeline_execution),
        }
        if projection:
            params['ProjectionExpression'] = projection
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        if limit:
            params['Limit'] = limit

        all_items = []
        last_key = None
        while True:
            if last_key:
                params['ExclusiveStartKey'] = last_key
            response = self.table.query(**params)
            all_items.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                break
        return all_items

    # ── GSI: date-pipeline-index ──────────────────────────────────────────

    def query_by_date(self, date: str, max_items: int = None,
                      filter_expr=None, projection: str = None,
                      expr_names: dict = None, key_condition=None,
                      select: str = None) -> list:
        """Query date-pipeline-index GSI with optional filters.
        
        Uses query_all helper for automatic pagination.
        """
        params = {
            'IndexName': 'date-pipeline-index',
            'KeyConditionExpression': key_condition or Key('date').eq(date),
        }
        if filter_expr:
            params['FilterExpression'] = filter_expr
        if projection:
            params['ProjectionExpression'] = projection
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        if select:
            params['Select'] = select

        if max_items:
            return query_all(self.table, max_items=max_items, **params)
        
        # Single-page query
        response = self.table.query(**params)
        return response.get('Items', [])

    def query_runs_by_pipeline(self, pipeline_name: str, min_runs: int = 15,
                               before_date: str = None, projection: str = None,
                               expr_names: dict = None) -> tuple:
        """Task rows for one pipeline, newest date first, via pipeline-date-index.

        The inverse of :meth:`query_by_date`: instead of asking "who ran on day D"
        (which forces callers to loop a window of days), this asks "this pipeline's
        runs, newest first" in one indexed query. Depth is therefore bounded only by
        the row TTL — there is no window constant to pick.

        Reads **whole dates** so an execution's task set is never split across a page;
        the caller can aggregate safely. Stops once ``min_runs`` distinct executions
        are covered.

        Returns ``(items, next_before_date)``. Pass ``next_before_date`` back as
        ``before_date`` for the next page (inclusive — the cut date is re-read, its
        runs were not returned). ``None`` means nothing older exists.
        """
        key = Key('pipeline_name').eq(pipeline_name)
        if before_date:
            key = key & Key('date').lte(before_date)

        params = {
            'IndexName': 'pipeline-date-index',
            'KeyConditionExpression': key,
            'ScanIndexForward': False,  # newest date first
        }
        if projection:
            params['ProjectionExpression'] = projection
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names

        items: list = []
        by_date: dict = {}
        last_key = None
        while True:
            if last_key:
                params['ExclusiveStartKey'] = last_key
            response = self.table.query(**params)
            for item in response.get('Items', []):
                items.append(item)
                date_str = item.get('date', '')
                pipeline_execution = item.get('pipeline_execution')
                if date_str and pipeline_execution:
                    by_date.setdefault(date_str, set()).add(pipeline_execution)
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return items, None  # nothing older exists

            # Only cut on a date boundary, and only once a second date is in the
            # buffer — otherwise the oldest date may still be half-read.
            total_runs = sum(len(execs) for execs in by_date.values())
            if total_runs >= min_runs and len(by_date) > 1:
                oldest = min(by_date)
                kept = [i for i in items if i.get('date', '') > oldest]
                return kept, oldest

    def query_runs_by_date(self, date: str, min_rows: int, projection: str = None,
                           expr_names: dict = None, filter_expr=None,
                           key_condition=None) -> list:
        """Task rows for one date, newest-first-by-nothing, via date-pipeline-index.

        The mirror of :meth:`query_runs_by_pipeline`, and the read behind the
        cross-pipeline feed. That one reads **whole dates**; this one reads **whole
        pipelines** — and for the same reason: an execution's task set must never be
        split.

        Splitting matters more here than "some rows are missing" suggests. ``/api/runs``
        derives a run's status from the task rows it got (ADR #112), so handing back half
        a run's tasks does not drop a run — it renders a **wrong** one: a failed run whose
        failing task fell past the cut reads as ``success``. The index is ordered by
        ``pipeline_name``, so a row-count cut lands mid-pipeline and does exactly that.

        Stops once ``min_rows`` rows are covered and drops the pipeline it stopped inside
        (it may be half-read). Keeps reading while only one pipeline is buffered, so a
        single busy pipeline is never the one dropped — otherwise its day would vanish
        whole. When the date fits in one DynamoDB page there is nothing to cut and the
        whole day comes back, ``min_rows`` or not.
        """
        params = {
            'IndexName': 'date-pipeline-index',
            'KeyConditionExpression': key_condition or Key('date').eq(date),
        }
        if projection:
            params['ProjectionExpression'] = projection
        if expr_names:
            params['ExpressionAttributeNames'] = expr_names
        if filter_expr:
            params['FilterExpression'] = filter_expr

        rows: list = []
        last_key = None
        while True:
            if last_key:
                params['ExclusiveStartKey'] = last_key
            response = self.table.query(**params)
            rows.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return rows          # whole date read — nothing is half-anything

            # Only cut on a pipeline boundary, and only once a second pipeline is in
            # the buffer — otherwise the cut pipeline is the only one and the day
            # comes back empty.
            if len(rows) >= min_rows and len({r.get('pipeline_name') for r in rows}) > 1:
                partial = rows[-1].get('pipeline_name')     # index order: the last one
                return [r for r in rows if r.get('pipeline_name') != partial]

    def query_by_date_raw(self, **query_params) -> dict:
        """Raw query on date-pipeline-index, returns full boto3 response.
        
        For cases needing LastEvaluatedKey, Count, etc.
        """
        query_params.setdefault('IndexName', 'date-pipeline-index')
        return self.table.query(**query_params)

    # ── Scan operations ───────────────────────────────────────────────────

    def scan(self, max_items: int = None, **kwargs) -> list:
        """Scan with optional pagination via scan_all helper."""
        if max_items:
            return scan_all(self.table, max_items=max_items, **kwargs)
        response = self.table.scan(**kwargs)
        return response.get('Items', [])

    def scan_raw(self, **kwargs) -> dict:
        """Raw scan, returns full boto3 response."""
        return self.table.scan(**kwargs)

    # ── Health check ──────────────────────────────────────────────────────

    def health_ping(self) -> dict:
        """Lightweight read for health check."""
        return self.table.get_item(
            Key={'execution_name': '_health_check_'},
            ConsistentRead=True
        )

    # ── Conditional check exception ───────────────────────────────────────

    @property
    def conditional_check_exception(self):
        """Access the ConditionalCheckFailedException for try/except."""
        return dynamodb.meta.client.exceptions.ConditionalCheckFailedException


# Singleton instance
executions_repo = ExecutionsRepo()
