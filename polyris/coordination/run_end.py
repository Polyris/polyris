"""How a run ends: its summary, its outcome and the cause of a failed run's answer (ADR #137 §4, ADR #138 §5).

A run has finished when every row is final. Each full pass counts the run's
rows per status and writes the counts on the run row when they changed: a
cache derived from facts, which the run lists read (Principle #42). It writes
them only when no pass that began reading later has written some, so a slow
pass never replaces a newer count (``SUMMARY_CONDITION``).

The outcome is ADR #112's derivation over the final rows, with the run row's
mark of a failed direct step: ``failed`` when a row failed or a step did,
``aborted`` when every final failure is a stop, an abort or an
``upstream_failed``, and ``success`` otherwise. The console's run lists show
the same outcome from the summary, and ``running`` with the count of the
run's failed tasks until then (``shown_status``, ``failed_tasks``).
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from ..constants import ExecutionStatus, TaskStatus, derive_execution_status
from . import run_row
from .transitions import FINAL

#: When the counts were read: a pass writes its summary only over an older one.
SUMMARY_AT = "summary_at"
SUMMARY_CONDITION = "attribute_not_exists(#summary_at) OR #summary_at <= :read_at"
SUMMARY_NAMES: Dict[str, str] = {"#summary": run_row.SUMMARY, "#summary_at": SUMMARY_AT}

#: Step Functions refuses a longer cause, and a refused answer fails for good.
CAUSE_MAX = 32_768


def summary(statuses: Iterable[str]) -> Dict[str, int]:
    """The count of a run's rows per status."""
    return dict(Counter(statuses))


def outcome(counts: Mapping[str, int], *, task_count: int, step_failed: bool) -> Optional[str]:
    """The run's outcome once every row of its ``task_count`` is final, or None while one is not or was not read.

    A read that missed rows is no answer (invariant 10): the counts must cover the whole run.
    """
    present = {status for status, n in counts.items() if n > 0}
    if sum(counts.values()) != task_count or not present or not present <= FINAL:
        return None
    if step_failed:
        return ExecutionStatus.FAILED
    return derive_execution_status(present)


#: Every status a run list shows: those ``shown_status`` gives, and ``stale``, a run from before 1.3 left unfinished
#: (the console's ``feed.runs_of_date``). A run list filtered by any other status holds no run (a backfill's status).
SHOWN_STATUSES = frozenset({ExecutionStatus.RUNNING, ExecutionStatus.SUCCESS, ExecutionStatus.FAILED,
                            ExecutionStatus.ABORTED, ExecutionStatus.STALE})


def shown_status(counts: Optional[Mapping[str, int]], *, step_failed: bool) -> str:
    """The run's status as the console shows it, from its run row's summary and step mark (§4).

    ``running`` until every row the summary counts is final, then the outcome. A run
    row without a summary yet, before the first full pass over its run, is running.
    """
    counts = counts or {}
    return outcome(counts, task_count=sum(counts.values()), step_failed=step_failed) or ExecutionStatus.RUNNING


def failed_tasks(counts: Optional[Mapping[str, int]]) -> int:
    """How many of the run's rows are ``failed``: what the console shows beside a run that is still running (§4).

    Only ``failed``, the status that makes the run ``failed`` once every row is final;
    a task that waits for a decision has not failed finally yet.
    """
    return int((counts or {}).get(TaskStatus.FAILED.value, 0))


def failure_cause(failed_tasks: Sequence[str], *, step_failure: Optional[str] = None) -> str:
    """The cause of ``SendTaskFailure`` for a failed run: the failed tasks, capped to fit Step Functions."""
    head = "; ".join(([f"step {step_failure} failed"] if step_failure else [])
                     + [f"{len(failed_tasks)} tasks failed"])
    names = sorted(failed_tasks)
    listed: List[str] = []
    length = len(head) + 2
    for i, name in enumerate(names):
        rest = len(names) - i - 1
        added = len(name) + (2 if listed else 0)
        if length + added + (len(f" and {rest} more") if rest else 0) > CAUSE_MAX:
            break
        listed.append(name)
        length += added
    more = len(names) - len(listed)
    return f"{head}: {', '.join(listed)}" + (f" and {more} more" if more else "") if names else head
