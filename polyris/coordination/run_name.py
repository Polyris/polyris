"""Names for pipeline runs started by coordination, and for ``run_task`` executions.

A run's task rows are keyed ``{task_id}-{date}-{last 20 characters of the run's
name}`` (``task_row.key``), so that suffix must be unique per run, or the rows of
different runs collide (ADR #130, defect B-X1). Every run name built here ends
with an 8-character hash of everything that identifies the run.

A ``run_task`` execution is named from its row (``run_task_name``, ADR #137
invariant 4), so a start repeated under the same name finds the first one.
"""

from __future__ import annotations

import hashlib
import re

#: AND push run: one per consumer pipeline and date.
KIND_AND = "and"
#: OR push run or ``cascade='all'``: one per event.
KIND_EVENT = "ev"
#: Backfill run: one per backfill, tier and date.
KIND_BACKFILL = "bf"
KINDS = frozenset({KIND_AND, KIND_EVENT, KIND_BACKFILL})

#: Step Functions execution names: letters, digits, '-' and '_', at most 80.
MAX_NAME = 80
_INVALID = re.compile(r"[^A-Za-z0-9_-]+")


def _digest(*parts: str, length: int) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:length]


def _clean(value: str) -> str:
    return _INVALID.sub("-", value).strip("-")


def run_name(pipeline: str, date: str, kind: str, discriminator: str = "") -> str:
    """Deterministic execution name for a run started by coordination.

    The same arguments always give the same name, so a repeated start returns
    ``ExecutionAlreadyExists`` instead of a second run. The pipeline part is
    truncated when needed; the date, kind and hash never are.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}; expected one of {sorted(KINDS)}")
    if not pipeline.strip() or not date.strip():
        raise ValueError("run_name needs a pipeline and a date")
    tail = f"-{_clean(date)}-{kind}-{_digest(pipeline, date, kind, discriminator, length=8)}"
    head = _clean(pipeline)[: MAX_NAME - len(tail)].rstrip("-")
    if not head:
        raise ValueError(f"pipeline name {pipeline!r} leaves no room in a run name")
    return head + tail


#: The hash at the end of a run_task execution's name: of the row key, the run's StartTime and the generation.
RUN_TASK_HASH = 16


def run_task_name(key: str, run_start: str, generation: int) -> str:
    """The name of a row's ``run_task`` execution (ADR #137 invariant 4).

    A prefix of the row key and a hash of the key, the run's ``StartTime`` and the
    generation, within Step Functions' 80 characters. One name per row and
    generation, so ``ExecutionAlreadyExists`` means the start was made; a Restart
    raises the generation, and with the start time in the hash, a run name used
    again once its old rows expired never meets an old execution.
    """
    digest = _digest(key, run_start, str(int(generation)), length=RUN_TASK_HASH)
    head = _clean(key)[: MAX_NAME - RUN_TASK_HASH - 1].rstrip("-")
    return f"{head}-{digest}" if head else digest
