"""
Constants for Console API

Centralized definitions for status names, trigger rules, and other constants
to ensure consistency across all Lambda functions and API routes.

v0.80.0 (ADR #83) — TaskStatus / TriggerRule / PipelineStatus / BackfillStatus
/ AssetOperator and the TASK_* / BACKFILL_* status sets are the single-source,
codegen-generated definitions (from polyris/constants.py), re-exported here.
The previous hand-maintained copies were an unguarded SSoT duplicate (the
drift check only validates constants_generated); their dead console-only
extras (TriggerRule.DEFAULT/EARLY_TRIGGER/WAIT_ALL, PipelineStatus.PAUSED/
ABORTED) were removed in the same pass. Only console-specific constants
(EventType, Limits, BackfillLimits, SFN_STATUS_MAP, validators) remain
defined here. Regenerate the imported names via `make generate-enums`.
"""
from constants_generated import (
    TaskStatus,
    TriggerRule,
    PipelineStatus,
    BackfillStatus,
    AssetOperator,
    BackfillUpstream,
    BACKFILL_ERROR_CODES,
    TASK_TERMINAL_STATUSES,
    TASK_SETTLED_STATUSES,
    TASK_SUCCESS_STATUSES,
    TASK_FAILURE_STATUSES,
    TASK_ACTIVE_STATUSES,
    TASK_WAITING_STATUSES,
    TASK_STOPPABLE_STATUSES,
    BACKFILL_TERMINAL_STATUSES,
    BACKFILL_ACTIVE_STATUSES,
    EXECUTION_STATUS_CANONICAL,
    _EXECUTION_STATUS_UPPERCASE_MAP,
    normalize_execution_status,
    derive_execution_status,
    reconcile_execution_status,
)


class EventType:
    """Event types for task_events table."""
    TASK_REGISTERED = 'TASK_REGISTERED'
    DEPS_READY = 'DEPS_READY'
    TASK_STARTED = 'TASK_STARTED'
    TASK_FINISHED = 'TASK_FINISHED'
    TASK_FAILED = 'TASK_FAILED'
    TASK_SKIPPED = 'TASK_SKIPPED'
    TASK_PAUSED = 'TASK_PAUSED'
    TASK_RESUMED = 'TASK_RESUMED'
    MANUAL_DECISION = 'MANUAL_DECISION'
    RETRY_SCHEDULED = 'RETRY_SCHEDULED'


# SFN status to internal status mapping
SFN_STATUS_MAP = {
    'RUNNING': TaskStatus.RUNNING,
    'SUCCEEDED': TaskStatus.SUCCESS,
    'FAILED': TaskStatus.FAILED,
    'TIMED_OUT': TaskStatus.FAILED,
    'ABORTED': TaskStatus.ABORTED,
    'PENDING_REDRIVE': TaskStatus.PENDING,
}


# Limits and defaults
class Limits:
    """System limits and defaults."""
    # Size limits
    MAX_RESULT_SIZE_BYTES = 200000  # 200KB before truncation
    MAX_ERROR_SIZE_BYTES = 50000   # 50KB before truncation
    
    # Query/Scan limits
    MAX_SCAN_ITEMS = 50000          # Default max for scan_all()
    MAX_FETCH_ITEMS = 10000         # Max items for get_all_tasks
    MAX_NOTIFICATIONS_SCAN = 5000   # Max items for notifications
    MAX_LOGS_ITEMS = 1000           # Max items for get_pipeline_logs
    MAX_RESTART_ITEMS = 5000        # Max items for restart_pipeline
    MAX_PIPELINES_TO_QUERY = 30
    MAX_STATS_ITEMS = 10000
    
    # Pagination
    RUNS_PAGE_SIZE = 15             # runs per page in the execution history dropdown
    RUNS_FEED_LIMIT = 50            # rows per page of the History runs feed
    TASKS_FEED_LIMIT = 100          # rows per page of the History tasks feed
    # Read budgets for a History feed's day. Floors, not caps: the read rounds up to
    # the next pipeline boundary, because the index is ordered by pipeline_name and
    # cutting mid-pipeline splits a run's task set — which /runs then derives a wrong
    # status from (ADR #113). A date that fits one DynamoDB page comes back whole.
    RUNS_MIN_ROWS_PER_DATE = 2000    # runs feed, one explicit date
    TASKS_MIN_ROWS_PER_DATE = 10000  # tasks feed, one explicit date
    FEED_MIN_ROWS_PER_DAY = 500      # either feed, per day of the fan-out

    # Time limits
    SLA_DAYS = 14
    PARALLEL_DATE_QUERIES = 14      # Worker cap when fanning date queries out one-per-day
    TTL_DAYS = 30
    TTL_QUEUED_DAYS = 7             # TTL for queued_asset_events
    MAX_NOTIFICATION_HOURS = 168    # Max 7 days for notifications
    
    # String limits
    EXECUTION_NAME_MAX_LENGTH = 80
    
    # Pagination
    DEFAULT_PAGE_SIZE = 50
    MAX_PAGE_SIZE = 100


class BackfillLimits:
    """Backfill operational limits (per ADR #51 Questions #8 + #9)."""
    PARTITION_SOFT_LIMIT = 500    # Preview warning above this
    # Hard ceiling at 1000 partitions per single backfill, derived from AWS
    # Step Functions Inline Map history-event limit:
    #   - ItemProcessor: 7 states, each ~2 events (entered + exited)
    #   - startExecution.sync:2 child SFN: ~4 events per iteration
    #   - Map iteration overhead: 2 events
    #   = ~20 history events per partition
    # AWS hard limit: 25,000 history events per execution.
    # Safe ceiling = 25000 / 20 = 1250; we use 1000 for headroom (allow some
    # retries/error paths). For backfills > 1000 partitions, user chunks
    # client-side. Distributed Map migration would lift this but adds
    # significant complexity — deferred until measured need (no user has
    # asked for >1000 in one shot).
    PARTITION_HARD_LIMIT = 1000
    MAX_PARALLEL = 10             # Map MaxConcurrency upper bound
    DEFAULT_PARALLEL = 5          # Default for backfill payload
    RECORD_TTL_DAYS = 30          # Same as executions for consistency
    # skip_completed pre-flight does one DDB Query per partition. Above
    # this threshold the pre-flight is bypassed (returns empty + warn) to
    # avoid API Gateway 29s timeout. Backfill still runs — just without
    # the skip optimization.
    PREFLIGHT_MAX_PARTITIONS = 100


# Sentinel pipeline_name for Backfill records in the pipeline-tokens table.
# Backfill records share the table with executions; this sentinel + record_type
# discriminator (per ADR #51) keeps them filtered out of execution queries.
BACKFILL_SENTINEL_PIPELINE_NAME = '_polyris_bulk_backfill'

