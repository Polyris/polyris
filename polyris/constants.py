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

# Statuses the coordination reconciler may act on (ADR #130 §2): the task is
# still waiting for its dependencies. Deliberately narrower than
# WAITING_STATUSES — deps_ready, waiting_delay and waiting_decision are past
# the dependency wait and belong to the wrapper / run_task.
RECONCILABLE_STATUSES: Set[str] = {
    TaskStatus.WAITING.value,
    TaskStatus.WAITING_PAUSED.value,
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


# Type alias for trigger_rule with IDE autocomplete: the nine rules of ADR #138,
# the same names as TriggerRule (a test keeps them equal). A rule reads its
# task's direct upstreams and their final outcomes; docs/features/DSL.md
# explains each one, and polyris/coordination/trigger_rules.py evaluates them.
TriggerRuleLiteral = Literal[
    "all_success",                  # Default. Every upstream succeeded, or an operator or the run skipped it.
    "one_success",                  # One upstream succeeded; runs at the first success.
    "all_done",                     # Every upstream is final, whatever the outcome.
    "all_skipped",                  # Every upstream was skipped.
    "none_skipped",                 # Every upstream is final and none was skipped.
    "one_failed",                   # One upstream failed finally; runs at the first final failure.
    "all_failed",                   # Every upstream failed finally.
    "none_failed",                  # Every upstream is final and none failed.
    "none_failed_min_one_success",  # Every upstream is final, none failed and one succeeded.
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
]


# AWS error codes whose fix is an IAM permission on the caller's role for the action it
# called. The deploy's resource check names that action; the console API raises them rather
# than folding them into "could not check" (CLAUDE.md, Error Visibility). A KMS denial is not
# here: its fix is kms:Decrypt, not the action that was called.
ACCESS_DENIED_CODES: frozenset[str] = frozenset({
    "AccessDenied",
    "AccessDeniedException",
    "UnauthorizedAccess",
    "AuthFailure",
})


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
    The nine trigger rules (ADR #138). A rule reads its task's direct
    upstreams: it runs the task as soon as its condition holds, and skips it
    or makes it ``upstream_failed`` only once every upstream is final. A failed
    task is final only after the decision on it (ADR #114). Validation rejects
    any other name, with a hint for the four that ADR #138 turned down
    (``trigger_rules.REJECTED``). See docs/features/DSL.md.
    """
    ALL_SUCCESS = "all_success"           # Every upstream succeeded, or an operator or the run skipped it (default)
    ONE_SUCCESS = "one_success"           # One upstream succeeded; does NOT wait for the others
    ALL_DONE = "all_done"                 # Every upstream is final, whatever the outcome (cleanup)
    ALL_SKIPPED = "all_skipped"           # Every upstream was skipped
    NONE_SKIPPED = "none_skipped"         # Every upstream is final and none was skipped
    ONE_FAILED = "one_failed"             # One upstream failed finally; does NOT wait for the others
    ALL_FAILED = "all_failed"             # Every upstream failed finally
    NONE_FAILED = "none_failed"           # Every upstream is final and none failed; skips pass
    NONE_FAILED_MIN_ONE_SUCCESS = "none_failed_min_one_success"  # As none_failed, and one upstream succeeded



class SkipOrigin:
    """Who skipped a task: a ``skipped`` row's ``skip_origin`` (ADR #138 §3).

    Only ``rule`` cascades under ``all_success``: a task that an operator or the
    run skipped lets its dependants run. The console's Skip, ``Start_Run`` and
    the dispatcher write these values (ADR #137 §3). A row skipped before 1.3
    may carry none, and passes as it did. Regenerate after a change
    (``make generate-enums``).
    """
    MANUAL = "manual"  # an operator, from the console
    RUN = "run"        # Start_Run, for a task named in the run's skip_tasks
    RULE = "rule"      # the dispatcher, because the task's own trigger rule can no longer be met
class ManualResolution:
    """
    The four actions an operator can take on a task via the Console UI.
    Written by ``console_api/routes/tasks.py::_write_synthetic_output_marker``
    as the ``_resolution`` field on the DDB marker, and read by the SDK
    (``xcom.py::XComManuallyResolvedError``) plus the UI
    (``manualResolution.ts``) — this class is the single source of truth for
    all three surfaces. Adding a fifth action means editing here and
    regenerating (``make generate-enums``); every consumer picks up the new
    value automatically.

    See ADR-123 §5 for the shipping contract.
    """
    MARK_SUCCESS = "mark_success"    # Operator declares the task succeeded (data verified out-of-band)
    SKIP = "skip"                    # Operator skips the task — downstream unblocked, no data produced
    FAIL = "fail"                    # Operator declares the task failed
    STOP = "stop"                    # Operator stopped a running task (may be restarted later)


class WarningKind:
    """What a warning in the console bell is about (ADR #130 §9).

    Written by the coordination Lambdas, the console, the SFN templates and the
    generated pipeline state machines as the ``kind`` of a ``_notify_warn_`` row
    (``records.warning_item``), and shown by the UI bell. Regenerate after a
    change (``make generate-enums``).
    """
    COORDINATION = "coordination"  # a wake-up path failed, registration could not finish, or a run could not be told how its task ended
    ZOMBIE = "zombie"              # a waiting task can no longer be woken; it was aborted
    PUSH = "push"                  # a pipeline scheduled on an asset could not be started
    SWEEPER = "sweeper"            # the sweeper woke a task whose wake-up hint was lost
    NOTIFY = "notify"              # notify_dependents failed after a task finished
    MANUAL = "manual"              # a manual action kept the task's real output instead of its marker
    FACT = "fact"                  # a task or a manual action could not record the asset facts it owed
    STEP = "step"                  # a direct step failed; its run fails once every branch has ended (ADR #137 §1)


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
    MAX_CAPACITY = "max_capacity"
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


class TaskSpecKey(str, Enum):
    """
    Keys of a task's spec in ``pipeline-specs`` (ADR #137 §8, §9): everything
    needed to run the task and to draw it. ``polyris-deploy`` writes a spec with
    these keys only, and ``Start_Run`` rejects a spec with any other
    (``polyris/coordination/task_spec.py``). A change to them changes the
    specs' shape, which needs a new ``task_spec.FORMAT_VERSION``.

    str-Enum: members compare and JSON-serialize as their plain string values.
    """
    TASK_ID = "task_id"
    TASK_TYPE = "task_type"
    TASK_ARN = "task_arn"
    TASK_CONFIG = "task_config"                      # ADR #108, retries included
    CROSS_ACCOUNT_ROLE = "cross_account_role"
    DEPENDENCIES = "dependencies"
    TRIGGER_RULE = "trigger_rule"
    EXECUTION_TIMEOUT = "execution_timeout"          # seconds, one attempt of the work
    ORCHESTRATION_TIMEOUT = "orchestration_timeout"  # seconds, a cap on all of the task's waiting
    WAIT_BEFORE = "wait_before"                      # seconds of countdown once ready
    OUTLETS = "outlets"
    INLETS = "inlets"
    WAIT_FOR = "wait_for"
    SKIP_ON_BACKFILL = "skip_on_backfill"
