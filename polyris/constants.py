"""
Constants and type definitions for SFN-DSL.
"""

from enum import Enum
from typing import FrozenSet, Literal, Set


# =============================================================================
# Task Status Lifecycle
# =============================================================================

class TaskStatus(str, Enum):
    """
    Task execution status lifecycle.
    
    Lifecycle diagram:
    
        ┌─────────────────────────────────────────────────────────────┐
        │                         WAITING                             │
        │  (initial state - waiting for dependencies or schedule)     │
        └─────────────────────────────────────────────────────────────┘
                    │
                    ▼
        ┌───────────────────┐     ┌───────────────────┐
        │    DEPS_READY     │     │  WAITING_PAUSED   │
        │ (deps satisfied)  │     │ (pipeline paused) │
        └───────────────────┘     └───────────────────┘
                    │
                    ▼
        ┌───────────────────┐
        │  WAITING_DELAY    │
        │ (countdown timer) │
        └───────────────────┘
                    │
                    ▼
        ┌───────────────────┐
        │      RUNNING      │
        │ (task executing)  │
        └───────────────────┘
                    │
        ┌───────────┴───────────┐
        ▼                       ▼
    ┌─────────┐           ┌─────────┐
    │ SUCCESS │           │ FAILED  │
    └─────────┘           └─────────┘
    
    Other terminal states:
    - SKIPPED: Task was skipped (manual or trigger rule)
    - ABORTED: Task was aborted (system)
    - UPSTREAM_FAILED: Upstream dependency failed (trigger rule blocked)
    
    Non-terminal stop state:
    - STOPPED: Task was stopped via UI (can be restarted)
    """
    # Active states
    WAITING = "waiting"                    # Initial: waiting for dependencies
    DEPS_READY = "deps_ready"              # Dependencies satisfied, ready to run
    WAITING_DELAY = "waiting_delay"        # In countdown timer (wait_before)
    WAITING_PAUSED = "waiting_paused"      # Pipeline is paused
    WAITING_DECISION = "waiting_decision"  # Waiting for manual decision
    RUNNING = "running"                    # Task is executing
    PENDING = "pending"                    # Pending redrive (SFN)
    
    # Terminal success states
    SUCCESS = "success"                    # Task completed successfully
    SUCCEEDED = "succeeded"                # Alias for success (legacy compat)
    
    # Terminal failure states
    FAILED = "failed"                      # Task execution failed
    UPSTREAM_FAILED = "upstream_failed"    # Blocked by upstream failure
    ABORTED = "aborted"                    # Task was aborted (system)
    
    # Terminal skip state
    SKIPPED = "skipped"                    # Task was skipped
    
    # Non-terminal stop state (can be restarted)
    STOPPED = "stopped"                    # Task was stopped via UI


# Status sets for common checks
# Note: STOPPED is NOT terminal - task can be restarted
TERMINAL_STATUSES: Set[str] = {
    TaskStatus.SUCCESS.value,
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.SKIPPED.value,
    TaskStatus.ABORTED.value,
    TaskStatus.UPSTREAM_FAILED.value,
}

# Terminal statuses plus 'stopped'. A "settled" task is one that will not change on
# its own — either it reached a terminal state, or it was deliberately stopped via the
# UI (restartable, non-terminal). Used wherever a stopped task must be preserved rather
# than treated as orphaned: task-feed row skipping and execution-abort reconciliation
# (so a deliberately-stopped task is not re-marked 'aborted'). Single source of truth —
# do not re-list these statuses inline.
SETTLED_STATUSES: Set[str] = TERMINAL_STATUSES | {TaskStatus.STOPPED.value}

SUCCESS_STATUSES: Set[str] = {
    TaskStatus.SUCCESS.value,
    TaskStatus.SUCCEEDED.value,
    TaskStatus.SKIPPED.value,  # Skipped is OK for downstream
}

FAILURE_STATUSES: Set[str] = {
    TaskStatus.FAILED.value,
    TaskStatus.UPSTREAM_FAILED.value,
    TaskStatus.ABORTED.value,
}

ACTIVE_STATUSES: Set[str] = {
    TaskStatus.RUNNING.value,
    TaskStatus.PENDING.value,
}

WAITING_STATUSES: Set[str] = {
    TaskStatus.WAITING.value,
    TaskStatus.DEPS_READY.value,
    TaskStatus.WAITING_DELAY.value,
    TaskStatus.WAITING_PAUSED.value,
    TaskStatus.WAITING_DECISION.value,
}

# Statuses that show countdown in UI
COUNTDOWN_STATUSES: Set[str] = {
    TaskStatus.DEPS_READY.value,
    TaskStatus.WAITING_DELAY.value,
}

# Statuses that can be stopped via UI
STOPPABLE_STATUSES: Set[str] = {
    TaskStatus.RUNNING.value,
    TaskStatus.PENDING.value,
    TaskStatus.WAITING.value,
    TaskStatus.WAITING_PAUSED.value,
    TaskStatus.WAITING_DELAY.value,
    TaskStatus.DEPS_READY.value,
    TaskStatus.WAITING_DECISION.value,
}


# =============================================================================
# Trigger Rules
# =============================================================================


# Type alias for trigger_rule with IDE autocomplete.
#
# ADR #114/#115/#117: polyris pauses a failed task for a human decision rather
# than propagating failure autonomously (intervention-first), and a CONFIRMED
# failure (resolved via Fail) cancels the whole pipeline's Parallel before any
# downstream trigger_rule ever evaluates. Given that, only 5 rule names produce
# a behavior reachable in practice; the other 6 either duplicate one of these 5
# exactly (verified across the full status-combination matrix, see
# tests/sdk/test_trigger_rules.py) or can never be satisfied at all
# (all_failed/one_failed — their only use case is reacting to a confirmed
# failure, which is exactly the state Parallel-abort prevents them from
# reaching). See ADR #117 and docs/features/DSL.md for the full analysis.
TriggerRuleLiteral = Literal[
    "all_success",           # Default. All upstream tasks succeeded.
    "one_success",           # At least one succeeded (doesn't wait for all).
    "all_done",              # All upstream tasks done (any status) — cleanup.
    "all_skipped",           # All upstream tasks were skipped.
    "none_skipped",          # No upstream task was skipped.
]


# Task type for service-specific execution
TaskTypeLiteral = Literal[
    "sfn",       # Nested Step Function (default)
    "lambda",    # Direct Lambda invocation
    "glue",      # Glue Job
    "ecs",       # ECS/Fargate task
    "athena",    # Athena query
    "emr",       # EMR step
    "batch",     # AWS Batch job
    "sagemaker", # SageMaker processing/training
]


# Schedule presets
SCHEDULE_PRESETS = {
    "@once": None,
    "@hourly": "rate(1 hour)",
    "@daily": "rate(1 day)",
    "@weekly": "rate(7 days)",
    "@monthly": "cron(0 0 1 * ? *)",
    "@yearly": "cron(0 0 1 1 ? *)",
    "@annually": "cron(0 0 1 1 ? *)",
}


class TriggerRule:
    """
    Trigger rule constants. 5 rule names, each producing a distinct,
    reachable behavior under polyris's intervention-first failure model
    (ADR #114): a failed task pauses for a human decision rather than
    propagating failure autonomously, and a *confirmed* failure cancels the
    whole pipeline's Parallel before any downstream trigger_rule evaluates.
    6 rule names were rejected (ADR #117) — they either duplicated one of
    these 5 exactly in every reachable state, or could never be satisfied at
    all. See docs/features/DSL.md for the analysis.
    """
    ALL_SUCCESS = "all_success"           # All upstream tasks have succeeded (default)
    ONE_SUCCESS = "one_success"           # At least one succeeded (does NOT wait for all)
    ALL_DONE = "all_done"                 # All upstream tasks are done with execution (cleanup)
    ALL_SKIPPED = "all_skipped"           # All upstream tasks are in skipped state
    NONE_SKIPPED = "none_skipped"         # No task skipped


# =============================================================================
# Pipeline-level statuses (v0.79.0 SSoT consolidation, ADR #72)
# =============================================================================

class PipelineStatus:
    """A pipeline card's status: its LAST run's canonical status, or 'idle' when the
    pipeline has no runs in the recent window (ADR #112, option c). The value set is
    ExecutionStatus plus 'idle' — kept in sync with ExecutionStatus."""
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ABORTED = "aborted"
    RECOVERED = "recovered"
    IDLE = "idle"


# =============================================================================
# ExecutionStatus — pipeline-execution-level status (SFN-aligned lowercase)
# Canonical from v0.78.14 (ADR #71); lifted into SDK at v0.79.0.
# =============================================================================

class ExecutionStatus:
    """Pipeline-execution status. Canonical lowercase form.

    AWS Step Functions returns these statuses in UPPERCASE; normalize at
    the boundary using `normalize_execution_status` before use. Canonical
    "success" (not "succeeded") system-wide per ADR #112 — supersedes ADR #71's
    value choice. `stopped` is a task-only status (restartable UI-stop) and is
    intentionally absent here: a stopped execution derives/reconciles to `aborted`.
    """
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    ABORTED = "aborted"
    RECOVERED = "recovered"  # Derived: SFN reports failed/timed_out but all tasks resolved


EXECUTION_STATUS_CANONICAL: Set[str] = {
    ExecutionStatus.RUNNING,
    ExecutionStatus.SUCCESS,
    ExecutionStatus.FAILED,
    ExecutionStatus.TIMED_OUT,
    ExecutionStatus.ABORTED,
    ExecutionStatus.RECOVERED,
}

_EXECUTION_STATUS_UPPERCASE_MAP = {
    'RUNNING': ExecutionStatus.RUNNING,
    'SUCCEEDED': ExecutionStatus.SUCCESS,   # SFN uppercase → canonical 'success' (ADR #112)
    'FAILED': ExecutionStatus.FAILED,
    'TIMED_OUT': ExecutionStatus.TIMED_OUT,
    'ABORTED': ExecutionStatus.ABORTED,
    # SFN never emits STOPPED for an execution; map defensively to aborted.
    'STOPPED': ExecutionStatus.ABORTED,
    # Legacy / task-form aliases for the success state → canonical 'success'.
    'SUCCESS': ExecutionStatus.SUCCESS,
    'success': ExecutionStatus.SUCCESS,
    'succeeded': ExecutionStatus.SUCCESS,
}


def normalize_execution_status(status, log_warn=None):
    """Normalize a pipeline-execution status to canonical lowercase.

    See ADR #71 for full reasoning. Idempotent on canonical inputs.
    """
    if status is None:
        return None
    if status in EXECUTION_STATUS_CANONICAL:
        return status
    mapped = _EXECUTION_STATUS_UPPERCASE_MAP.get(status)
    if mapped is not None:
        return mapped
    if log_warn is not None:
        log_warn("Unexpected execution status; cannot normalize",
                 status=status)
    return status


# Task-status groupings used to derive a pipeline-execution status (ADR #112).
# 'succeeded' is accepted as an input alias for the canonical 'success'.
_DERIVE_RESOLVED = {'success', 'succeeded', 'skipped'}
_DERIVE_FAILURE = {'failed'}
_DERIVE_STOPPED = {'stopped', 'aborted', 'upstream_failed'}
_DERIVE_TERMINAL = _DERIVE_RESOLVED | _DERIVE_FAILURE | _DERIVE_STOPPED


def derive_execution_status(task_statuses):
    """Derive a pipeline-execution status from its task statuses (DynamoDB only).

    The single source of this derivation (ADR #112). Returns one of the
    DynamoDB-derivable canonical values: 'running', 'success', 'failed', 'aborted'.
    ('timed_out' and 'recovered' are reconciliation-only — see
    ``reconcile_execution_status``.) A genuine task failure outranks an interruption;
    an interruption (stopped/aborted/upstream_failed) without a failure is 'aborted'.
    """
    statuses = set(task_statuses)
    if not statuses:
        return ExecutionStatus.RUNNING
    is_completed = statuses.issubset(_DERIVE_TERMINAL)
    has_failure = bool(statuses & _DERIVE_FAILURE)
    has_stopped = bool(statuses & _DERIVE_STOPPED)
    if is_completed:
        if has_stopped and not has_failure:
            return ExecutionStatus.ABORTED
        return ExecutionStatus.FAILED if has_failure else ExecutionStatus.SUCCESS
    if has_failure:
        return ExecutionStatus.FAILED
    if has_stopped:
        return ExecutionStatus.ABORTED
    return ExecutionStatus.RUNNING


def reconcile_execution_status(base, sfn_status, all_tasks_resolved):
    """Refine a derived 'running' status against the authoritative SFN status (ADR #112).

    Only a 'running' base is reconciled — terminal derivations are trusted. ``sfn_status``
    must already be canonical (see ``normalize_execution_status``). When SFN reports a
    failure/timeout but every task resolved, the execution is 'recovered'.
    """
    if base != ExecutionStatus.RUNNING:
        return base
    if not sfn_status or sfn_status == ExecutionStatus.RUNNING:
        return base
    if sfn_status in (ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT) and all_tasks_resolved:
        return ExecutionStatus.RECOVERED
    return sfn_status


# =============================================================================
# Backfill-level statuses + enums
# =============================================================================

class BackfillStatus:
    """Backfill status — aggregate of N partition outcomes (ADR #54)."""
    PENDING = "pending"      # record created; bulk SFN not yet running Map
    RUNNING = "running"      # Map iterating; at least one partition in flight
    COMPLETED = "completed"  # all attempted partitions succeeded
    FAILED = "failed"        # all attempted partitions failed (zero succeeded)
    PARTIAL = "partial"      # some succeeded, some failed (mixed outcome)
    CANCELED = "canceled"    # user-initiated cancel via /api/backfills/{id}/cancel


BACKFILL_TERMINAL_STATUSES: Set[str] = {
    BackfillStatus.COMPLETED,
    BackfillStatus.FAILED,
    BackfillStatus.PARTIAL,
    BackfillStatus.CANCELED,
}

BACKFILL_ACTIVE_STATUSES: Set[str] = {
    BackfillStatus.PENDING,
    BackfillStatus.RUNNING,
}


class BackfillCascade:
    """Asset backfill cascade mode — how downstream consumers are handled."""
    AUTO = "auto"  # Default: trigger downstreams that depend on the affected asset
    ALL = "all"    # Trigger entire downstream subgraph
    NONE = "none"  # Don't trigger anything downstream


class BackfillUpstream:
    """Asset backfill upstream lineage build mode (ADR #92).

    Mirrors BackfillCascade on the upstream side. Codegen-generated into
    constants_generated.py (backend) and ui/src/generated/enums.ts (UI) so the
    backend validator and the TS type derive from this one definition rather
    than hand-maintained copies (ADR #94)."""
    OFF = "off"      # Default: build only the producer (input assumed present)
    SMART = "smart"  # Build missing same-pipeline ancestors (frontier stops at present output)
    FORCE = "force"  # Rebuild the full same-pipeline lineage regardless of presence


# Canonical registry of backfill error codes returned by the backfill route to
# the client. Codegen-generated into ui/src/generated/enums.ts; the UI friendly
# error map (utils/backfillErrors.ts) must cover every code here, gated by
# backfillErrors.test.ts (keys ⊇ codes). The registry itself is pinned to the
# route's emitted literals by test_backfill_error_registry (registry == emitted)
# so neither side silently drifts (ADR #94). Unlike the enum value objects of
# ADR #93, the code→friendly-text map is hand-authored content and cannot be
# generated away; it can only be gated — hence a registry rather than a single
# generated map.
BACKFILL_ERROR_CODES: FrozenSet[str] = frozenset({
    "already_terminal",
    "child_name_too_long",
    "concurrent_backfill_active",
    "granularity_override_not_allowed",
    "id_space_exhausted",
    "internal_error",
    "invalid_downstream",
    "invalid_downstream_for_pipeline_target",
    "invalid_granularity_override",
    "invalid_options",
    "invalid_partition_format",
    "invalid_partition_keys",
    "invalid_partitions",
    "invalid_target",
    "invalid_target_type",
    "invalid_tasks",
    "invalid_upstream",
    "invalid_upstream_for_pipeline_target",
    "malformed_body",
    "malformed_parent",
    "misconfigured",
    "multi_producer_asset",
    "no_producer",
    "not_eligible",
    "not_found",
    "nothing_to_retry",
    "nothing_to_run",
    "partition_keys_not_failed",
    "producer_pipeline_missing",
    "range_too_large",
    "sfn_start_failed",
    "status_race",
    "target_not_found",
    "throttled",
    "unreachable_target_type",
    "upstream_cycle",
})


class BackfillGranularity:
    """Backfill partition cadence (ADR #50)."""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


# =============================================================================
# Asset staleness
# =============================================================================

class StalenessStatus:
    """Asset freshness vs declared SLA window."""
    FRESH = "fresh"
    WARNING = "warning"
    STALE = "stale"
    UNKNOWN = "unknown"


# =============================================================================
# Asset trigger operator
# =============================================================================

class AssetOperator:
    """How multiple asset dependencies combine to trigger downstream."""
    AND = "AND"  # All assets required
    OR = "OR"    # Any asset triggers


# =============================================================================
# task_config contract (SDK writer <-> run_task wrapper reader) — ADR #108
# =============================================================================

class TaskConfigKey(str, Enum):
    """
    Keys of the per-task `task_config` dict the SDK passes to the run_task
    wrapper. This is the single source of truth for BOTH sides of the contract:
    the writer (generators) keys its dicts with these members, and the template
    contract test asserts every `task_config.<key>` the wrapper reads is
    declared here. Add a key here first; a bare string literal on either side
    fails tests/sdk/test_task_config_contract.py.

    str-Enum: members compare and JSON-serialize as their plain string values.
    """
    # lambda
    FUNCTION_NAME = "function_name"
    PAYLOAD = "payload"
    # glue
    JOB_NAME = "job_name"
    ARGUMENTS = "arguments"
    WORKER_TYPE = "worker_type"
    NUMBER_OF_WORKERS = "number_of_workers"
    ALLOCATED_CAPACITY = "allocated_capacity"
    # ecs
    CLUSTER = "cluster"
    TASK_DEFINITION = "task_definition"
    LAUNCH_TYPE = "launch_type"
    SUBNETS = "subnets"
    SECURITY_GROUPS = "security_groups"
    ASSIGN_PUBLIC_IP = "assign_public_ip"
    OVERRIDES = "overrides"
    # athena
    QUERY_STRING = "query_string"
    DATABASE = "database"
    OUTPUT_LOCATION = "output_location"
    WORKGROUP = "workgroup"
    # emr
    CLUSTER_ID = "cluster_id"
    STEP = "step"
    # batch
    JOB_DEFINITION = "job_definition"
    JOB_QUEUE = "job_queue"
    PARAMETERS = "parameters"
    # retry policy (ADR #107)
    RETRIES = "retries"
    RETRY_DELAY = "retry_delay"
    RETRY_BACKOFF = "retry_backoff"
    MAX_RETRY_DELAY = "max_retry_delay"
    RETRY_JITTER = "retry_jitter"
