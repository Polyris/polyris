"""Centralized DDB schema constants per CLAUDE.md #13.

The rule: any DDB field name referenced from production code MUST be
imported from here, not hardcoded as a string literal. Tests must also
import these constants — that way a single rename triggers a failing
test, not a silent runtime miss (as the v0.78 audit caught with the
`pipeline_status` → `status` field-name bug).

Scope: v0.78 ships constants for fields touched by the unified Backfill
flow (per ADR #51). Older subsystems still use string literals; that's a
v0.79+ housekeeping pass, not v0.78 scope. New code MUST use these
constants going forward.

Pattern:
    from dal.ddb_schema import PipelineTokens, BackfillStatus

    # Reader:
    if item.get(PipelineTokens.STATUS) == BackfillStatus.SUCCESS:
        ...
    # Writer:
    item[PipelineTokens.STATUS] = BackfillStatus.SUCCESS

If a SAM template renames a field, the test using the same constant
fails. Magic-string drift is impossible.
"""


class PipelineTokens:
    """Fields on the pipeline-tokens DDB table.

    PK: execution_name (S)
    GSIs:
      - backfill-id-index (backfill_id, HASH)
      - date-pipeline-index (date HASH, pipeline_name RANGE)
      - pipeline-execution-index (pipeline_execution HASH)
      - parent-execution-index (parent_execution_id HASH)
    """
    # ── Identity / keys ────────────────────────────────────────────────────
    EXECUTION_NAME = 'execution_name'  # PK
    BACKFILL_ID = 'backfill_id'  # GSI key (sparse — only on Backfill records)
    PIPELINE_EXECUTION = 'pipeline_execution'  # GSI key — pipeline-level execution UUID
    PARENT_EXECUTION_ID = 'parent_execution_id'  # GSI key — parent SFN execution (was run_id)
    DATE = 'date'  # GSI partition key
    PIPELINE_NAME = 'pipeline_name'  # GSI sort key
    TASK_NAME = 'task_name'

    # ── Status (task-level row; pipeline-tokens stores task rows) ─────────
    # Per v0.78 audit: this is 'status', NOT 'pipeline_status'. A handler
    # that filtered on 'pipeline_status' would silently never match.
    STATUS = 'status'

    # ── Record type discriminator ─────────────────────────────────────────
    # Per ADR #51: distinguishes Backfill records (record_type='backfill')
    # from regular task rows. Use BACKFILL_SENTINEL_PIPELINE_NAME for the
    # pipeline_name on Backfill records too.
    RECORD_TYPE = 'record_type'

    # ── Timestamps ────────────────────────────────────────────────────────
    STARTED_AT = 'started_at'
    FINISHED_AT = 'finished_at'
    TTL = 'ttl'

    # ── Backfill-specific (ADR #51) ───────────────────────────────────────
    TARGET_SEED = 'target_seed'
    TARGET_PIPELINE = 'target_pipeline'
    TASK_SUBSET = 'task_subset'
    PARTITION_KEY = 'partition_key'
    PARTITION_KEYS = 'partition_keys'
    TOTAL_PARTITIONS = 'total_partitions'
    COMPLETED_PARTITIONS = 'completed_partitions'
    FAILED_PARTITIONS = 'failed_partitions'
    SKIPPED_PARTITIONS = 'skipped_partitions'
    CASCADE = 'cascade'
    OPTIONS = 'options'
    STARTED_BY = 'started_by'
    PIPELINE_DAG_HASH = 'pipeline_dag_hash'
    PARENT_BACKFILL_ID = 'parent_backfill_id'  # retry-failed lineage


class TaskStatus:
    """Allowed values for PipelineTokens.STATUS on task-level rows.

    Used by SFN templates when writing, by route handlers when filtering.
    Adding a new value here without updating SFN templates → bug;
    removing one without updating handlers → bug. Tests on this constant
    + snapshot tests on SFN templates pin the contract.
    """
    SUCCESS = 'success'
    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    RUNNING = 'running'
    WAITING = 'waiting'
    WAITING_DECISION = 'waiting_decision'
    UPSTREAM_FAILED = 'upstream_failed'
    ABORTED = 'aborted'
    PAUSED = 'paused'

    SUCCESSFUL = frozenset({SUCCESS, SUCCEEDED, SKIPPED})  # treated as "done" for skip_completed
