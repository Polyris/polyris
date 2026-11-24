"""
Data Access Layer: Pipeline Executions Table.

Encapsulates all DynamoDB operations for the pipeline-executions table
(TABLE_NAME / pipeline_tokens). Used by routes: executions, tasks, slack,
pipelines, health, notifications, backfill.

Table schema:
    PK: execution_name (str)
    GSIs: pipeline-execution-index (pipeline_execution), date-pipeline-index (date, pipeline_name),
    pipeline-date-index (pipeline_name, date), run-start-index (start_day, started_at: run rows only)

Every read of the two date indexes goes through this repository, and drops the
internal rows and the backfill records with ``should_skip_token_row``: backfill
records carry a ``date`` (ADR #137 §5), so both indexes list them. A census test
(``tests/test_date_index_readers.py``) fails for a read of them anywhere else.
"""

from typing import Any, Optional

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from polyris.constants import BACKFILLING_INDEX_MESSAGE
from polyris.coordination import attention as att, run_row
from config import dynamodb, dynamodb_client, TABLE_NAME
from utils import query_all, should_skip_token_row

#: The two indexes keyed by a task's date, whose every read skips internal and backfill rows.
DATE_INDEXES = ('date-pipeline-index', 'pipeline-date-index')
#: What ``should_skip_token_row`` reads, which a projection that names other fields must name too.
_SKIP_FIELDS = ('execution_name', 'pipeline_name')


class IndexBuilding(Exception):
    """A read of an index DynamoDB is still building: an upgrade added it, and the same read succeeds once it is
    built (ADR #137 §5; about 8 minutes on a table of 1.2's size). Transient: the console says so."""


def _building(exc: ClientError) -> bool:
    error = exc.response.get('Error', {})
    return error.get('Code') == 'ValidationException' and BACKFILLING_INDEX_MESSAGE in str(error.get('Message', ''))


def _skippable(params: dict) -> dict:
    """``params`` with the fields ``should_skip_token_row`` reads added to its projection, when it has one."""
    projection = params.get('ProjectionExpression')
    if not projection:
        return params
    names = params.get('ExpressionAttributeNames') or {}
    named = {names.get(path.strip(), path.strip()) for path in projection.split(',')}
    missing = [f for f in _SKIP_FIELDS if f not in named]
    if missing:
        params['ProjectionExpression'] = ', '.join([projection, *missing])
    return params


def _task_rows(items: list) -> list:
    """The task rows of a date index's read: no internal row and no backfill record."""
    return [item for item in items if not should_skip_token_row(item)]


class ExecutionsRepo:
    """Repository for pipeline-executions (tokens) table."""

    #: Raised by a read of an index DynamoDB is still building (``runs_started``); a route catches it here.
    IndexBuilding = IndexBuilding

    def __init__(self):
        self._table_name = TABLE_NAME

    @property
    def table(self):
        """Lazy table reference (new on every access for Lambda reuse safety)."""
        return dynamodb.Table(self._table_name)

    # ── Single-item operations ────────────────────────────────────────────

    #: Run fields no task index projects (sam/template.yaml).
    RUN_META_FIELDS = ('triggered_by', 'attempt', 'restarts', 'retry_attempt', 'task_config')

    def batch_get_run_meta(self, execution_names: list[str],
                           fields: tuple[str, ...] = RUN_META_FIELDS) -> dict[str, dict]:
        """Fetch `fields` (by default triggered_by, attempt, restarts, retry_attempt, task_config)
        from the base table for a batch of execution_names. Used after GSI reads whose
        NonKeyAttributes don't include them. Returns {execution_name: {field: value, …}}.

        retry_attempt (ADR #125) is the in-SFN auto-retry index; without it in this
        projection the History feed would show 0 for every row even on actively
        retrying tasks.

        Chunks at 100 (AWS limit) and retries UnprocessedKeys automatically.
        """
        unique = [n for n in set(execution_names) if n]
        if not unique:
            return {}
        # Every field goes through a placeholder: `attempt` is a DynamoDB reserved word.
        names = {f'#f{i}': field for i, field in enumerate(fields)}
        projection = ', '.join(['execution_name', *names])
        result: dict[str, dict] = {}
        for i in range(0, len(unique), 100):
            pending = {
                self._table_name: {
                    'Keys': [{'execution_name': n} for n in unique[i:i + 100]],
                    'ProjectionExpression': projection,
                    'ExpressionAttributeNames': names,
                }
            }
            while pending:
                response = dynamodb.batch_get_item(RequestItems=pending)
                for item in response.get('Responses', {}).get(self._table_name, []):
                    result[item['execution_name']] = item
                pending = response.get('UnprocessedKeys') or {}
        return result

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
                                     limit: int = None,
                                     filter_expr=None) -> list:
        """Query pipeline-execution-index GSI. Returns all pages; ``filter_expr`` keeps the rows it matches."""
        params = {
            'IndexName': 'pipeline-execution-index',
            'KeyConditionExpression': Key('pipeline_execution').eq(pipeline_execution),
        }
        if filter_expr is not None:
            params['FilterExpression'] = filter_expr
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
                      expr_names: dict = None, key_condition=None) -> list:
        """Task rows of date-pipeline-index with optional filters; internal and backfill rows dropped.

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
        _skippable(params)

        if max_items:
            return _task_rows(query_all(self.table, max_items=max_items, **params))

        # Single-page query
        response = self.table.query(**params)
        return _task_rows(response.get('Items', []))

    def rows_of_pipeline_date(self, pipeline_name: str, date: str) -> list:
        """One pipeline's task rows of one date, every page, from pipeline-date-index; internal and backfill rows
        dropped. The index projects only part of a row: what tells the date's runs apart and orders them
        (``pipeline_execution``, ``started_at``, ``status``). A query of one pipeline's date, where a scan of the
        whole table stood (ADR #137 §5)."""
        params: dict = {
            'IndexName': 'pipeline-date-index',
            'KeyConditionExpression': Key('pipeline_name').eq(pipeline_name) & Key('date').eq(date),
        }
        return _task_rows(query_all(self.table, **params))

    def query_runs_by_date(self, date: str, min_rows: Optional[int], projection: str = None,
                           expr_names: dict = None, filter_expr=None,
                           key_condition=None) -> list:
        """Task rows for one date, newest-first-by-nothing, via date-pipeline-index.

        The read behind a date filter. It reads **whole pipelines**: an execution's task set
        must never be split.

        Splitting matters more here than "some rows are missing" suggests. ``/api/runs``
        derives a run's status from the task rows it got (ADR #112), so handing back half
        a run's tasks does not drop a run — it renders a **wrong** one: a failed run whose
        failing task fell past the cut reads as ``success``. The index is ordered by
        ``pipeline_name``, so a row-count cut lands mid-pipeline and does exactly that.

        Stops once ``min_rows`` rows are covered and drops the pipeline it stopped inside
        (it may be half-read); ``None`` reads the whole date. Keeps reading while only one pipeline is buffered, so a
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
        _skippable(params)

        rows: list = []
        last_key = None
        while True:
            if last_key:
                params['ExclusiveStartKey'] = last_key
            response = self.table.query(**params)
            rows.extend(_task_rows(response.get('Items', [])))
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return rows          # whole date read — nothing is half-anything

            # Only cut on a pipeline boundary, and only once a second pipeline is in
            # the buffer — otherwise the cut pipeline is the only one and the day
            # comes back empty.
            if min_rows is not None and len(rows) >= min_rows and len({r.get('pipeline_name') for r in rows}) > 1:
                partial = rows[-1].get('pipeline_name')     # index order: the last one
                return [r for r in rows if r.get('pipeline_name') != partial]

    def query_by_date_raw(self, **query_params) -> dict:
        """One page of date-pipeline-index, as boto3 returns it, for a caller that pages itself.

        ``Items`` hold task rows only: internal and backfill rows are dropped,
        while ``Count`` and ``ScannedCount`` stay DynamoDB's. A ``Select='COUNT'``
        would count those rows too, so it is refused.
        """
        if query_params.get('Select') == 'COUNT':
            raise ValueError("a count of date-pipeline-index would include internal and backfill rows; "
                             "read the items and count the task rows")
        query_params.setdefault('IndexName', 'date-pipeline-index')
        if query_params['IndexName'] not in DATE_INDEXES:
            raise ValueError(f"query_by_date_raw reads the date indexes only, not {query_params['IndexName']}")
        response = self.table.query(**_skippable(query_params))
        response['Items'] = _task_rows(response.get('Items', []))
        return response

    def recent_warnings(self, since: str, limit: int) -> list:
        """Warning rows (`records.warning_item`) written since `since` (UTC, `…Z`), newest first.

        Read from the attention index by write time (ADR #130 §9), across its
        16 `warn#` shards, so a warning about a task of any partition date
        reaches the bell (defect B-N1). The index projects what the bell shows.
        """
        return self.attention_entries(att.WARN, since=since)[:limit]

    def attention_entries(self, kind: str, *, since: str | None = None) -> list:
        """Every entry of ``kind`` in the attention index, across its 16 shards, newest first by ``attention_at``,
        and only those at or after ``since`` (UTC, ``…Z``) when it is given.

        The bell reads the ``failed`` entries of its window, by when each task failed, and every ``decision``
        entry, by its deadline, whatever the date of the run (ADR #137 §6, D92); the warnings by when each was
        written. The index projects what the bell shows (``attention.PROJECTION``).
        """
        rows: list = []
        for partition in att.partitions(kind):
            key: Any = Key(att.KEY).eq(partition)
            if since:
                key = key & Key(att.DUE).gte(since)
            rows.extend(query_all(self.table, IndexName=att.INDEX, KeyConditionExpression=key,
                                  ScanIndexForward=False))
        rows.sort(key=lambda r: str(r.get(att.DUE, '')), reverse=True)
        return rows

    # ── GSI: run-start-index ──────────────────────────────────────────────

    def runs_started(self, start_day: str, *, before: str | None = None, pipeline: str | None = None,
                     run_dates: tuple[str, str] | None = None, want: int | None = None) -> list:
        """Run rows whose execution started on ``start_day`` (UTC), newest first, from run-start-index (ADR #137 §5).

        ``before`` keeps the runs that started before it (a ``started_at``: the feeds' cursor), ``pipeline`` one
        pipeline's, and ``run_dates`` those whose logical date lies between its two dates, inclusive. ``want``
        stops the read at the end of the page that brings that many rows; the page's other rows come back too,
        so no run that started at the same instant as the last one wanted is cut off (``feed.page_by_started_at``
        keeps such twins together). The index holds run rows only and projects what the run lists show
        (``run_row.INDEX_FIELDS``). Raises ``IndexBuilding`` while DynamoDB builds the index.
        """
        key: Any = Key(run_row.START_DAY).eq(start_day)
        if before:
            key = key & Key(run_row.STARTED_AT).lt(before)
        params: dict = {'IndexName': run_row.START_INDEX, 'KeyConditionExpression': key, 'ScanIndexForward': False}
        condition: Any = None
        if pipeline:
            condition = Attr(run_row.PIPELINE).eq(pipeline)
        if run_dates:
            dates = Attr(run_row.RUN_DATE).between(*run_dates)
            condition = dates if condition is None else condition & dates
        if condition is not None:
            params['FilterExpression'] = condition
        rows: list = []
        while True:
            try:
                response = self.table.query(**params)
            except ClientError as exc:
                if _building(exc):
                    raise IndexBuilding(str(exc)) from exc
                raise
            rows.extend(response.get('Items', []))
            last_key = response.get('LastEvaluatedKey')
            if not last_key or (want is not None and len(rows) >= want):
                return rows
            params['ExclusiveStartKey'] = last_key

    # ── The one scan ──────────────────────────────────────────────────────

    def discover_pipeline_names(self, limit: int = 1000) -> list:
        """The pipeline names on one page of the table: the pipelines list's fallback when the registry is empty
        (ADR #136 §2). The console's only scan of the tasks table, and one page of at most ``limit`` items,
        whatever the table's size: every other read is a query of an index or a get by key (ADR #137 §5;
        ``tests/test_no_table_scans.py``)."""
        response = self.table.scan(ProjectionExpression='execution_name, pipeline_name', Limit=limit)
        return sorted({str(item['pipeline_name']) for item in response.get('Items', [])
                       if item.get('pipeline_name') and item['pipeline_name'] != 'unknown'
                       and not should_skip_token_row(item)})

    # ── Health check ──────────────────────────────────────────────────────

    def health_ping(self) -> dict:
        """Lightweight read for health check."""
        return self.table.get_item(
            Key={'execution_name': '_health_check_'},
            ConsistentRead=True
        )

    def transact_write(self, items: list) -> None:
        """One transaction of the items a coordination builder made: low-level, typed, each naming its table
        (an operator's action, ADR #137 §3), sent through the low-level client: the resource's own client would
        serialize them again. Raises ClientError; a refused condition cancels it as a whole
        (``transaction_canceled_exception``)."""
        dynamodb_client.transact_write_items(TransactItems=items)

    @property
    def transaction_canceled_exception(self):
        """The TransactionCanceledException a refused condition raises, for try/except."""
        return dynamodb_client.exceptions.TransactionCanceledException


# Singleton instance
executions_repo = ExecutionsRepo()
