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
(EventType, Limits, BackfillLimits, validators) remain
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
    derive_execution_status,
)


class EventType:
    """Event types for task_events table.

    `TASK_STARTED` fires once per run_task execution, on its first dispatch.
    `TASK_RETRY_STARTED` (ADR #125) fires on each in-SFN retry re-dispatch inside the
    same execution. Count "task starts" from `TASK_STARTED` only; sum both for total
    dispatches.
    """
    TASK_REGISTERED = 'TASK_REGISTERED'
    DEPS_READY = 'DEPS_READY'
    DEPS_SKIPPED = 'DEPS_SKIPPED'
    DEPS_BLOCKED = 'DEPS_BLOCKED'
    TASK_STARTED = 'TASK_STARTED'
    TASK_RETRY_STARTED = 'TASK_RETRY_STARTED'
    TASK_FINISHED = 'TASK_FINISHED'
    TASK_FAILED = 'TASK_FAILED'
    TASK_SKIPPED = 'TASK_SKIPPED'
    TASK_PAUSED = 'TASK_PAUSED'
    TASK_RESUMED = 'TASK_RESUMED'
    MANUAL_DECISION = 'MANUAL_DECISION'
    RETRY_SCHEDULED = 'RETRY_SCHEDULED'  # reserved — no SFN state emits this yet



# Limits and defaults
class Limits:
    """System limits and defaults."""
    # Query/Scan limits
    MAX_SCAN_ITEMS = 50000          # Default max for scan_all()
    MAX_FETCH_ITEMS = 10000         # Max items for get_all_tasks
    MAX_LOGS_ITEMS = 1000           # Max items for get_pipeline_logs
    
    # Pagination
    RUNS_PAGE_SIZE = 15             # runs per page in the execution history dropdown
    RUNS_FEED_LIMIT = 50            # rows per page of the History runs feed
    TASKS_FEED_LIMIT = 100          # rows per page of the History tasks feed (whole runs: a page may hold more)
    # The task rows one page of the Tasks tab reads at most, by its runs' summaries: about 5 s at the TR-7
    # estimate's 0.24-0.29 ms a row, under API Gateway's 30 s. The page then ends at a run, with its cursor.
    TASKS_READ_BUDGET = 20000

    # Time limits
    SLA_DAYS = 14  # the pipelines list's window: ui/src/utils/historyWindow.ts (parity test there)
    # The start days the Runs and Tasks tabs and a pipeline's runs read from run-start-index (ADR #137 §5, D140): as long as
    # Step Functions keeps an execution's history. ui/src/utils/historyWindow.ts names it (parity test there).
    RUNS_WINDOW_DAYS = 90
    PARALLEL_DATE_QUERIES = 14      # Worker cap when fanning date queries out one-per-day
    MAX_NOTIFICATION_HOURS = 168    # Max 7 days for notifications
    
    # String limits
    EXECUTION_NAME_MAX_LENGTH = 80


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

