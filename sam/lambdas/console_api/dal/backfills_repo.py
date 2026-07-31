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
    GSI: backfill-id-index (PK: backfill_id) — used for child execution lookup

Per ADR #56 — six statuses: pending, running, completed, failed,
partial, canceled.
"""

from datetime import datetime, timezone
from typing import Optional

from boto3.dynamodb.conditions import Key, Attr
from config import dynamodb, TABLE_NAME
from utils import scan_all, query_all


# Sentinel value for Backfill record's pipeline_name field. Distinguishes
# Backfill records from regular pipeline executions. Filtered out by
# /runs and /executions list endpoints.
BACKFILL_SENTINEL_PIPELINE_NAME = "_polyris_bulk_backfill"

# Per ADR #51 — RECORD_TTL_DAYS same as executions for consistency.
BACKFILL_TTL_SECONDS = 30 * 24 * 60 * 60


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
        item.setdefault('pipeline_name', BACKFILL_SENTINEL_PIPELINE_NAME)
        item.setdefault('record_type', 'backfill')
        item.setdefault('ttl', int(datetime.now(timezone.utc).timestamp()) + BACKFILL_TTL_SECONDS)
        self.table.put_item(Item=item)

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
        item.setdefault('pipeline_name', BACKFILL_SENTINEL_PIPELINE_NAME)
        item.setdefault('record_type', 'backfill')
        item.setdefault('ttl', int(datetime.now(timezone.utc).timestamp()) + BACKFILL_TTL_SECONDS)
        try:
            self.table.put_item(
                Item=item,
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
        per ADR #54).

        Returns the updated item attributes.
        """
        kwargs = {
            'Key': {'execution_name': backfill_id},
            'UpdateExpression': 'SET #status = :s, updated_at = :ts',
            'ExpressionAttributeNames': {'#status': 'status'},
            'ExpressionAttributeValues': {
                ':s': status,
                ':ts': datetime.now(timezone.utc).isoformat(),
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

        ``limit=None`` returns every match. The scan below is unconditional, so ``limit``
        buys no capacity — it only truncates an already-materialised list. A caller that
        filters afterwards (the unified feed does, per ADR #95's per-kind semantics) must
        pass ``None`` and slice itself, or the slice spends the page on records the
        filter is about to drop.

        The cursor is applied **before** ``limit``, and must be: this method sorts
        newest-first, so filtering *after* the slice would hand every page but the
        first an empty result — the newest ``limit`` Backfills are exactly the ones
        any cursor has already served.

        Currently implemented as a scan with sentinel filter. At >10K
        Backfill records this becomes expensive; future optimization is
        to add a started_at GSI (out of scope for v0.78). The cursor filter runs in
        memory for the same reason it stays out of the FilterExpression: DynamoDB
        applies filters after reading, so pushing it down saves no capacity — and it
        would silently drop Backfills with no ``started_at`` instead of sorting them
        last, which is where the rest of the feed puts them.
        """
        items = scan_all(
            self.table,
            FilterExpression=Attr('pipeline_name').eq(BACKFILL_SENTINEL_PIPELINE_NAME),
        )
        if before:
            items = [i for i in items if (i.get('started_at') or '') < before]
        # Sort by started_at desc; missing started_at sorts last
        items.sort(key=lambda x: x.get('started_at', ''), reverse=True)
        return items if limit is None else items[:limit]

    def list_active(self) -> list:
        """List Backfills with status in (pending, running).

        Used for /api/backfills?status=active and health endpoint.
        """
        items = scan_all(
            self.table,
            FilterExpression=(
                Attr('pipeline_name').eq(BACKFILL_SENTINEL_PIPELINE_NAME)
                & Attr('status').is_in(['pending', 'running'])
            ),
        )
        return items

    def list_active_for_pipeline(self, target_pipeline: str) -> list:
        """List active (pending/running) Backfills targeting a specific pipeline.

        Used by start_backfill to enforce ``options.allow_concurrent=false`` —
        if any active Backfill exists for the same target_pipeline, the new
        start is rejected to avoid duplicate work and race conditions on
        per-partition records.
        """
        items = scan_all(
            self.table,
            FilterExpression=(
                Attr('pipeline_name').eq(BACKFILL_SENTINEL_PIPELINE_NAME)
                & Attr('status').is_in(['pending', 'running'])
                & Attr('target_pipeline').eq(target_pipeline)
            ),
        )
        return items

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

        Implementation: scan with filter on parent_backfill_id. Backfills
        are infrequent (~hundreds per pipeline per year) so a scan is fine
        at this scale; if retry rates spike, add a parent-backfill-id GSI.
        """
        items = scan_all(
            self.table,
            FilterExpression=(
                Attr('pipeline_name').eq(BACKFILL_SENTINEL_PIPELINE_NAME)
                & Attr('parent_backfill_id').eq(parent_backfill_id)
            ),
        )
        items.sort(key=lambda x: x.get('started_at', ''))
        return items


# Singleton instance
backfills_repo = BackfillsRepo()
