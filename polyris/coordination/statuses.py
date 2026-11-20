"""Task status sets used by the coordination logic (ADR #130 §2, §3).

Derived from :mod:`polyris.constants`, the single source of truth.
"""

from ..constants import (
    RECONCILABLE_STATUSES,
    SUCCESS_STATUSES,
    TERMINAL_STATUSES,
    TaskStatus,
    WarningKind,
)

__all__ = [
    "RECONCILABLE", "TERMINAL", "SUCCESS_ONLY",
    "SKIPPED", "WAITING", "WAITING_PAUSED", "ABORTED", "FAILED", "WARNING_KINDS",
]

#: The waiting statuses: a decision moves a task row only from one of these.
RECONCILABLE = frozenset(RECONCILABLE_STATUSES)
#: Statuses after which a task never changes on its own.
TERMINAL = frozenset(TERMINAL_STATUSES)
#: "Ran and succeeded" — includes the legacy ``succeeded`` alias, excludes ``skipped``.
SUCCESS_ONLY = frozenset(SUCCESS_STATUSES - {TaskStatus.SKIPPED.value})
SKIPPED = TaskStatus.SKIPPED.value
WAITING = TaskStatus.WAITING.value
WAITING_PAUSED = TaskStatus.WAITING_PAUSED.value
ABORTED = TaskStatus.ABORTED.value
FAILED = TaskStatus.FAILED.value
#: Every `kind` a warning row may carry (polyris.constants.WarningKind).
WARNING_KINDS = frozenset(v for k, v in vars(WarningKind).items() if not k.startswith("_"))
