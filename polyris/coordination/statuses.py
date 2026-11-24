"""Task status sets used by the coordination logic (ADR #130 §2, §3).

Derived from :mod:`polyris.constants`, the single source of truth.
"""

from ..constants import (
    RECONCILABLE_STATUSES,
    SUCCESS_STATUSES,
    TaskStatus,
    WarningKind,
)

__all__ = ["RECONCILABLE", "SUCCESS_ONLY", "FAILED", "WARNING_KINDS"]

#: The waiting statuses: a decision moves a task row only from one of these.
RECONCILABLE = frozenset(RECONCILABLE_STATUSES)
#: "Ran and succeeded" — includes the legacy ``succeeded`` alias, excludes ``skipped``.
SUCCESS_ONLY = frozenset(SUCCESS_STATUSES - {TaskStatus.SKIPPED.value})
FAILED = TaskStatus.FAILED.value
#: Every `kind` a warning row may carry (polyris.constants.WarningKind).
WARNING_KINDS = frozenset(v for k, v in vars(WarningKind).items() if not k.startswith("_"))
