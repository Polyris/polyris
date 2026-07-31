"""
Shared Constants for Lambda Functions

This is the SINGLE SOURCE OF TRUTH for status constants across all Lambdas.
When updating these values, ensure all Lambdas that use them are updated.

IMPORTANT: Lambda functions cannot share code directly. Each Lambda that needs
these constants should either:
1. Copy this file into their directory during build
2. Use a Lambda Layer (recommended for production)
3. Manually keep their local constants in sync with this file

Lambdas using these constants:
- console_api (has own copy in constants.py)
- evaluate_deps (has own copy inline)
- check_assets (doesn't need - only checks asset freshness)
- notify_asset_subscribers (doesn't need - only notifies)
"""


class TaskStatus:
    """Task execution status values.

    v0.79.5 (ADR #77) — class-level sets moved to module-level
    constants generated from polyris/constants.py.
    """
    WAITING = 'waiting'
    WAITING_PAUSED = 'waiting_paused'
    WAITING_DELAY = 'waiting_delay'
    DEPS_READY = 'deps_ready'
    RUNNING = 'running'
    PENDING = 'pending'
    SUCCESS = 'success'
    FAILED = 'failed'
    SKIPPED = 'skipped'
    STOPPED = 'stopped'  # Non-terminal: task can be restarted
    ABORTED = 'aborted'
    UPSTREAM_FAILED = 'upstream_failed'
    WAITING_DECISION = 'waiting_decision'


# v0.79.5 (ADR #77) — module-level sets (re-exported from generated).
# Lambda using this _shared copy also needs constants_generated.py
# in its deploy package; `make sync-constants` handles that copy.
# Import lazily so import-time failure (missing generated file) is
# diagnosable from the _shared copy.
try:
    from constants_generated import (
        TASK_TERMINAL_STATUSES,
        TASK_SUCCESS_STATUSES,
        TASK_FAILURE_STATUSES,
        TASK_ACTIVE_STATUSES,
        TASK_WAITING_STATUSES,
        TASK_STOPPABLE_STATUSES,
    )
except ImportError:
    # Single source of truth: when the generated file is absent (e.g. a direct
    # unit test run against the repo), fall back to the SDK canonical sets rather
    # than re-listing values here. No hand-maintained duplicates.
    from polyris.constants import (
        TERMINAL_STATUSES as TASK_TERMINAL_STATUSES,
        SUCCESS_STATUSES as TASK_SUCCESS_STATUSES,
        FAILURE_STATUSES as TASK_FAILURE_STATUSES,
        ACTIVE_STATUSES as TASK_ACTIVE_STATUSES,
        WAITING_STATUSES as TASK_WAITING_STATUSES,
        STOPPABLE_STATUSES as TASK_STOPPABLE_STATUSES,
    )


class TriggerRule:
    """5 trigger rules (ADR #117)."""
    ALL_SUCCESS = 'all_success'
    ONE_SUCCESS = 'one_success'
    ALL_DONE = 'all_done'
    ALL_SKIPPED = 'all_skipped'
    NONE_SKIPPED = 'none_skipped'


class AssetOperator:
    """Asset dependency operators."""
    AND = 'AND'  # All assets required
    OR = 'OR'    # Any asset triggers
