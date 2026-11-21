"""
Data Access Layer: Backfill records.

Backfill records live in the same pipeline-tokens table as executions
(schemaless DDB), distinguished by sentinel ``pipeline_name`` and
``record_type='backfill'``. This repo encapsulates all Backfill-specific
read/write patterns.

Per ADR #51 (Question #4/A) — separate repo from executions_repo for
single responsibility and future-proofing.

Table schema (pipeline-tokens):
    PK: execution_name = backfill_id (e.g., "bf-a1b2c3d4")
    Sentinel: pipeline_name = "_polyris_bulk_backfill"
    Discriminator: record_type = "backfill"
    date: the UTC day the backfill started (ADR #137 §5)
    GSI: backfill-id-index (PK: backfill_id) — used for child execution lookup
    GSI: pipeline-date-index (pipeline_name, date) — the search of the records

Per ADR #56 — six statuses: pending, running, completed, failed,
partial, canceled.

Every search reads ``pipeline-date-index`` with ``pipeline_name`` = the sentinel,
newest day first, and the records in full with ``BatchGetItem``, since that index
projects few of their fields: the tasks table keeps a run's history for a year
and 90 days (ADR #137 §5), so a scan of it no longer fits the console's 30 s.
A record lives as long as a run's history while its backfill runs, and 90 days
once its final status is written (``retention``). A record written before 1.3
has no ``date``: no search finds it, and it expires 30 days after its creation.
"""

from datetime import datetime, timezone
from typing import List, Optional, Set

from boto3.dynamodb.conditions import Key
from config import dynamodb, TABLE_NAME
from constants import BACKFILL_TERMINAL_STATUSES
from polyris.coordination import retention
from utils import query_all


# Sentinel value for Backfill record's pipeline_name field. Distinguishes
# Backfill records from regular pipeline executions. Filtered out by
# /runs and /executions list endpoints.
BACKFILL_SENTINEL_PIPELINE_NAME = "_polyris_bulk_backfill"

#: The index the search reads, and how many keys one BatchGetItem takes.
SEARCH_INDEX = 'pipeline-date-index'
BATCH_GET = 100
#: A backfill that did not get its records read after this many rounds of UnprocessedKeys fails the request.
BATCH_ROUNDS = 8


def _stamped(item: dict) -> dict:
    """A new record with its sentinel, its kind, the day it started and the TTL of a backfill that runs."""
    now = datetime.now(timezone.utc)
    item.setdefault('pipeline_name', BACKFILL_SENTINEL_PIPELINE_NAME)
    item.setdefault('record_type', 'backfill')
    item.setdefault('date', str(item.get('started_at') or now.isoformat())[:10])
    item.setdefault('ttl', retention.ttl(now, retention.BACKFILL_RUNNING))
    return item


class BackfillsRepo:
    """Repository for Backfill records in the pipeline-tokens table."""

    def __init__(self):
        self._table_name = TABLE_NAME

    @property
    def table(self):
        """Lazy table reference (new on every access for Lambda reuse safety)."""
        return dynamodb.Table(self._table_name)

    # ── Single-item operations ────────────────────────────────────────────

    def get(self, backfill_id: str, consistent: bool = False) -> Optional[dict]:
        """Get a single Backfill record by ID. Returns None if not found
        or if the record at this key is not a Backfill (defensive)."""
        kwargs = {'Key': {'execution_name': backfill_id}}
        if consistent:
            kwargs['ConsistentRead'] = True
        response = self.table.get_item(**kwargs)
        item = response.get('Item')
        if item is None:
            return None
        # Defensive: ensure this is actually a Backfill record. A bug or
        # ID collision shouldn't return an unrelated execution.
        if item.get('record_type') != 'backfill':
            return None
        return item

    def put(self, item: dict) -> None:
        """Put a Backfill record. Required fields are not enforced here
        (caller's responsibility); see ADR #51 'Backfill record shape'
        for the canonical schema."""
        # Ensure sentinel + discriminator are always set, even if caller
        # forgot. Defense-in-depth against accidental misclassification.
        self.table.put_item(Item=_stamped(item))

    def put_if_new(self, item: dict) -> bool:
        """Conditional put: only succeeds if no item with this execution_name
        exists. Returns True on success, False on collision.

        Used to detect ID collisions (per ADR #51) — the 32-bit ID space
        from `bf-{uuid4.hex[:8]}` has a non-trivial birthday probability
        at production volumes (~10% at 30k retained records), so the
        caller must retry with a fresh ID on False.

        Raises on real DDB errors (throttling, IAM, table-missing) so the
        caller can surface 5xx rather than silently retry forever.
        """
        from botocore.exceptions import ClientError
        try:
            self.table.put_item(
                Item=_stamped(item),
                ConditionExpression='attribute_not_exists(execution_name)',
            )
            return True
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') == 'ConditionalCheckFailedException':
                return False
            raise

    def delete(self, backfill_id: str) -> None:
        """Delete a Backfill record. Generally not used in practice; TTL
        handles expiry. Available for tests and manual cleanup."""
        self.table.delete_item(Key={'execution_name': backfill_id})

    def update_status(
        self,
        backfill_id: str,
        status: str,
        condition_status_in: Optional[list] = None,
    ) -> dict:
        """Update Backfill status. Optional ``condition_status_in`` lets
        the caller atomically check current status (used by cancel flow
        per ADR #54). A final status gives the record its last 90 days
        (ADR #137 §5), as ``bulk_backfill`` does when it ends.

        Returns the updated item attributes.
        """
        now = datetime.now(timezone.utc)
        final = status in BACKFILL_TERMINAL_STATUSES
        kwargs = {
            'Key': {'execution_name': backfill_id},
            'UpdateExpression': 'SET #status = :s, updated_at = :ts' + (', #ttl = :ttl' if final else ''),
            'ExpressionAttributeNames': {'#status': 'status', **({'#ttl': 'ttl'} if final else {})},
            'ExpressionAttributeValues': {
                ':s': status,
                ':ts': now.isoformat(),
                **({':ttl': retention.ttl(now, retention.BACKFILL_ENDED)} if final else {}),
            },
            'ReturnValues': 'ALL_NEW',
        }
        if condition_status_in:
            # Build IN clause with placeholders
            placeholders = []
            for idx, s in enumerate(condition_status_in):
                placeholder = f":expected_{idx}"
                kwargs['ExpressionAttributeValues'][placeholder] = s
                placeholders.append(placeholder)
            kwargs['ConditionExpression'] = f"#status IN ({', '.join(placeholders)})"
        return self.table.update_item(**kwargs).get('Attributes', {})

    def increment_counter(self, backfill_id: str, counter_name: str, by: int = 1) -> None:
        """Atomic counter increment for completed/failed/skipped partitions.
        Used by bulk-backfill SFN Map iterations.

        ``counter_name`` must be one of: completed_partitions,
        failed_partitions, skipped_partitions.
        """
        if counter_name not in (
            'completed_partitions', 'failed_partitions', 'skipped_partitions',
        ):
            raise ValueError(
                f"Invalid counter name {counter_name!r}; must be one of "
                f"completed_partitions, failed_partitions, skipped_partitions"
            )
        self.table.update_item(
            Key={'execution_name': backfill_id},
            UpdateExpression=f"ADD {counter_name} :inc",
            ExpressionAttributeValues={':inc': by},
        )

    # ── Query operations ──────────────────────────────────────────────────

    def list_recent(self, limit: Optional[int] = 50, before: str = None) -> list:
        """List most recent Backfills, sorted by started_at desc.

        ``before`` is a ``started_at`` cursor — only Backfills older than it are
        returned. This is the History feed's paging contract (see
        ``console_api/feed.py``), which merges Backfills into ``/api/runs`` as
        first-class rows (ADR #95).

        ``limit=None`` returns every match. A caller that filters afterwards (the
        unified feed does, per ADR #95's per-kind semantics) must pass ``None`` and
        slice itself, or the slice spends the page on records the filter is about
        to drop. With a ``limit``, the search stops on a day boundary once it has
        that many records, and reads no older day.

        The cursor is applied **before** ``limit``, and must be: this method sorts
        newest-first, so filtering *after* the slice would hand every page but the
        first an empty result. A record without ``started_at`` sorts last, as the
        rest of the feed puts it.
        """
        items = self._records(before_day=before[:10] if before else None, enough=limit)
        if before:
            items = [i for i in items if (i.get('started_at') or '') < before]
        # Sort by started_at desc; missing started_at sorts last
        items.sort(key=lambda x: x.get('started_at', ''), reverse=True)
        return items if limit is None else items[:limit]

    def list_active(self) -> list:
        """List Backfills with status in (pending, running).

        Used for /api/backfills?status=active and health endpoint.
        """
        return [i for i in self._records() if i.get('status') in ('pending', 'running')]

    def list_active_for_pipeline(self, target_pipeline: str) -> list:
        """List active (pending/running) Backfills targeting a specific pipeline.

        Used by start_backfill to enforce ``options.allow_concurrent=false`` —
        if any active Backfill exists for the same target_pipeline, the new
        start is rejected to avoid duplicate work and race conditions on
        per-partition records.
        """
        return [i for i in self.list_active() if i.get('target_pipeline') == target_pipeline]

    def query_child_executions(self, backfill_id: str) -> list:
        """List all executions belonging to a Backfill.

        Uses ``backfill-id-index`` GSI (defined in template.yaml). Sparse
        GSI — only executions with ``backfill_id`` attribute set are
        indexed. The Backfill record itself is also indexed (its
        ``backfill_id`` attribute equals its ``execution_name``); callers
        wanting only child executions should filter out the parent record.
        """
        return query_all(
            self.table,
            IndexName='backfill-id-index',
            KeyConditionExpression=Key('backfill_id').eq(backfill_id),
        )

    def list_retries_of(self, parent_backfill_id: str) -> list:
        """List Backfills that were created as a retry of the given one.

        Walks the parent_backfill_id chain — returns direct children only,
        not transitive descendants. Used by the retry chain UI (ADR #68).
        """
        items = [i for i in self._records() if i.get('parent_backfill_id') == parent_backfill_id]
        items.sort(key=lambda x: x.get('started_at', ''))
        return items

    # ── The search ────────────────────────────────────────────────────────

    def _records(self, *, before_day: Optional[str] = None, enough: Optional[int] = None) -> List[dict]:
        """The records, read in full, of the days on or before ``before_day``, newest day first.

        With ``enough``, the read stops once it holds that many keys and a
        whole older day: no day is half read, so the newest records are all there.
        The keys of ``before_day`` itself count toward none of them, since the
        caller's cursor may drop any of them.
        """
        condition = Key('pipeline_name').eq(BACKFILL_SENTINEL_PIPELINE_NAME)
        if before_day:
            condition = condition & Key('date').lte(before_day)
        params = {'IndexName': SEARCH_INDEX, 'KeyConditionExpression': condition, 'ScanIndexForward': False,
                  'ProjectionExpression': 'execution_name, #d', 'ExpressionAttributeNames': {'#d': 'date'}}
        keys: List[str] = []
        days: Set[str] = set()
        counted = 0
        last_key = None
        while True:
            if last_key:
                params['ExclusiveStartKey'] = last_key
            response = self.table.query(**params)
            for item in response.get('Items', []):
                day = str(item.get('date', ''))
                if enough is not None and counted >= enough and day not in days:
                    return self._read_full(keys)          # a whole older day after enough: stop here
                days.add(day)
                keys.append(str(item['execution_name']))
                counted += day != before_day
            last_key = response.get('LastEvaluatedKey')
            if not last_key:
                return self._read_full(keys)

    def _read_full(self, keys: List[str]) -> List[dict]:
        """The records by key with BatchGetItem, in chunks, until DynamoDB has answered for every key."""
        found: List[dict] = []
        for start in range(0, len(keys), BATCH_GET):
            pending = {self._table_name: {'Keys': [{'execution_name': k} for k in keys[start:start + BATCH_GET]]}}
            for _ in range(BATCH_ROUNDS):
                response = dynamodb.batch_get_item(RequestItems=pending)
                found.extend(response.get('Responses', {}).get(self._table_name, []))
                pending = response.get('UnprocessedKeys') or {}
                if not pending:
                    break
            else:
                raise RuntimeError(f"DynamoDB left backfill records unread after {BATCH_ROUNDS} rounds")
        return [item for item in found if item.get('record_type') == 'backfill'
                or item.get('pipeline_name') == BACKFILL_SENTINEL_PIPELINE_NAME]


# Singleton instance
backfills_repo = BackfillsRepo()
