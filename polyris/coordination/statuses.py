"""Task status sets used by the coordination logic (ADR #130 §2, §3).

Derived from :mod:`polyris.constants`, the single source of truth. The Lambda
copy of this package gets a literal version of this module from
``python -m polyris.codegen.sync_coordination``.
"""

from ..constants import (
    FAILURE_STATUSES,
    RECONCILABLE_STATUSES,
    SUCCESS_STATUSES,
    TERMINAL_STATUSES,
    TaskStatus,
)

__all__ = [
    "RECONCILABLE", "TERMINAL", "SUCCESS_ONLY", "FAILURE",
    "SKIPPED", "WAITING", "WAITING_PAUSED", "ABORTED", "FAILED",
]

#: The reconciler acts only on rows in these statuses.
RECONCILABLE = frozenset(RECONCILABLE_STATUSES)
#: Statuses after which a task never changes on its own.
TERMINAL = frozenset(TERMINAL_STATUSES)
#: "Ran and succeeded" — includes the legacy ``succeeded`` alias, excludes ``skipped``.
SUCCESS_ONLY = frozenset(SUCCESS_STATUSES - {TaskStatus.SKIPPED.value})
#: Real failures, as trigger rules count them.
FAILURE = frozenset(FAILURE_STATUSES)
SKIPPED = TaskStatus.SKIPPED.value
WAITING = TaskStatus.WAITING.value
WAITING_PAUSED = TaskStatus.WAITING_PAUSED.value
ABORTED = TaskStatus.ABORTED.value
FAILED = TaskStatus.FAILED.value
