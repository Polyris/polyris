"""The nine trigger rules over a task's direct upstreams (ADR #138).

One implementation: the engine decides with ``evaluate``, the console shows
each upstream's verdict with it, and ``polyris-local`` runs it (§6). A rule
reads final outcomes (§2):

- final: a status in ``SETTLED_STATUSES``; every other status is pending,
  ``waiting_decision`` included, so no rule fires on a failure that is not final;
- a final failure: ``failed``, ``upstream_failed``, ``stopped``, and
  ``aborted`` on rows from before 1.3;
- a skip: every ``skipped`` row, whoever skipped it. Only ``all_success``
  reads the origin: a skip by a rule cascades, one by an operator or the run
  passes (§3).

A rule runs its task as soon as its condition holds, and skips it or makes it
``upstream_failed`` only once every upstream is final. An unknown name fails
its task, a task without upstreams included.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Sequence

from ..constants import SkipOrigin, TaskStatus, TriggerRule
from .statuses import SUCCESS_ONLY
from .transitions import FINAL, FINAL_FAILURES

#: The engine's rules: the members of ``TriggerRule``, which the validation and the UI's type share.
KNOWN_RULES = frozenset(v for k, v in vars(TriggerRule).items() if not k.startswith("_"))

#: Names that validation rejects, each with what to use instead (§1).
REJECTED: Dict[str, str] = {
    "one_done": "'all_done' to wait for every upstream; 'one_success' or 'one_failed' to react to the first "
                "outcome of one kind",
    "all_done_min_one_success": "'one_success', which starts at the first success instead of waiting for the rest",
    "always": "no rule: remove the dependency, since a task without upstreams starts with its run",
    "all_done_setup_success": "'all_done' for a cleanup task",
}

#: Rules that need success or no failure: once all upstreams are final and the rule is not met, a final
#: failure among them makes the task ``upstream_failed``; otherwise it is skipped (ADR #115).
FAILURE_AVERSE_RULES = frozenset({TriggerRule.ALL_SUCCESS, TriggerRule.ONE_SUCCESS,
                                  TriggerRule.NONE_FAILED, TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS})

READY = "ready"
WAIT = "wait"
SKIP = "skip"
UPSTREAM_FAILED = "upstream_failed"
#: An unknown rule: the task fails with the cause, whatever its upstreams (§2).
FAIL = "fail"

_SKIPPED = TaskStatus.SKIPPED.value


@dataclass(frozen=True)
class Counts:
    total: int
    success: int
    failed: int        # final failures
    skipped: int       # every skip
    cascading: int     # skips by a rule, which only all_success reads
    pending: int


@dataclass(frozen=True)
class DepEvaluation:
    """A rule's outcome over its upstreams' current statuses."""
    verdict: str
    reason: str
    rule: str
    counts: Counts


#: What an upstream counts as for every rule (``count``): the console shows it beside the rule's verdict, so no copy
#: of the rules reads the statuses again (ADR #137 §1).
COUNTS_AS_SUCCESS, COUNTS_AS_FAILED, COUNTS_AS_SKIPPED_BY_RULE, COUNTS_AS_SKIPPED, COUNTS_AS_PENDING = (
    "success", "failed", "skipped_by_rule", "skipped", "pending")


def counts_as(status: str, skip_origin: Optional[str] = None) -> str:
    """The category ``count`` puts an upstream of ``status`` in; a final status no category names counts as done
    for ``all_done`` alone, as ``failed``'s complement, and shows as ``pending`` never."""
    if status in SUCCESS_ONLY:
        return COUNTS_AS_SUCCESS
    if status in FINAL_FAILURES:
        return COUNTS_AS_FAILED
    if status == _SKIPPED:
        return COUNTS_AS_SKIPPED_BY_RULE if skip_origin == SkipOrigin.RULE else COUNTS_AS_SKIPPED
    if status not in FINAL:
        return COUNTS_AS_PENDING
    return COUNTS_AS_FAILED   # pragma: no cover — every final status is a success, a failure or a skip


def count(statuses: Sequence[str], skip_origins: Optional[Sequence[Optional[str]]] = None) -> Counts:
    """Count upstream statuses the way every rule reads them; ``skip_origins`` is aligned with ``statuses``."""
    origins = _aligned(statuses, skip_origins, "skip_origins")
    return Counts(
        total=len(statuses),
        success=sum(1 for s in statuses if s in SUCCESS_ONLY),
        failed=sum(1 for s in statuses if s in FINAL_FAILURES),
        skipped=sum(1 for s in statuses if s == _SKIPPED),
        cascading=sum(1 for s, o in zip(statuses, origins) if s == _SKIPPED and o == SkipOrigin.RULE),
        pending=sum(1 for s in statuses if s not in FINAL),
    )


#: When each rule runs its task. Some fire while upstreams are pending: one_success at the first
#: success, one_failed at the first final failure; every other rule needs every upstream final.
_MET: Dict[str, Callable[[Counts], bool]] = {
    TriggerRule.ALL_SUCCESS: lambda c: c.pending == 0 and c.success + c.skipped - c.cascading == c.total,
    TriggerRule.ONE_SUCCESS: lambda c: c.success >= 1,
    TriggerRule.ALL_DONE: lambda c: c.pending == 0,
    TriggerRule.ALL_SKIPPED: lambda c: c.pending == 0 and c.skipped == c.total,
    TriggerRule.NONE_SKIPPED: lambda c: c.pending == 0 and c.skipped == 0,
    TriggerRule.ONE_FAILED: lambda c: c.failed >= 1,
    TriggerRule.ALL_FAILED: lambda c: c.pending == 0 and c.failed == c.total,
    TriggerRule.NONE_FAILED: lambda c: c.pending == 0 and c.failed == 0,
    TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS: lambda c: c.pending == 0 and c.failed == 0 and c.success >= 1,
}

#: Why a rule that every final upstream left unmet skips its task.
_UNMET: Dict[str, Callable[[Counts], str]] = {
    TriggerRule.ALL_SUCCESS: lambda c: f"{c.cascading} upstreams were skipped by their own rule",
    TriggerRule.ONE_SUCCESS: lambda c: "no upstream succeeded",
    TriggerRule.ALL_SKIPPED: lambda c: f"not every upstream was skipped ({c.skipped} of {c.total})",
    TriggerRule.NONE_SKIPPED: lambda c: f"{c.skipped} upstreams were skipped",
    TriggerRule.ONE_FAILED: lambda c: "no upstream failed",
    TriggerRule.ALL_FAILED: lambda c: f"not every upstream failed ({c.failed} of {c.total})",
    TriggerRule.NONE_FAILED_MIN_ONE_SUCCESS: lambda c: "no upstream succeeded",
}


def evaluate(rule: str, statuses: Sequence[str],
             skip_origins: Optional[Sequence[Optional[str]]] = None) -> DepEvaluation:
    """The outcome of ``rule`` over its upstreams' ``statuses``, with the origin of each skip."""
    counts = count(statuses, skip_origins)
    if rule not in KNOWN_RULES:
        hint = f"; use {REJECTED[rule]}" if rule in REJECTED else f"; known rules: {sorted(KNOWN_RULES)}"
        return DepEvaluation(FAIL, f"unknown trigger_rule {rule!r}{hint}", rule, counts)
    if counts.total == 0:
        return DepEvaluation(READY, "no upstreams", rule, counts)
    if _MET[rule](counts):
        return DepEvaluation(READY, f"{rule} is met", rule, counts)
    if counts.pending > 0:
        # No count in it: a row's recorded reason changes only when its outcome may (ADR #137 §1, *Cost*).
        return DepEvaluation(WAIT, "waiting for its upstreams", rule, counts)
    if rule in FAILURE_AVERSE_RULES and counts.failed > 0:
        return DepEvaluation(UPSTREAM_FAILED, f"{counts.failed} upstreams failed", rule, counts)
    return DepEvaluation(SKIP, _UNMET[rule](counts), rule, counts)


def enabled_at(rule: str, statuses: Sequence[str], finished_at: Sequence[Optional[datetime]],
               skip_origins: Optional[Sequence[Optional[str]]] = None) -> Optional[datetime]:
    """When the upstreams first allowed the outcome ``evaluate`` gives (ADR #130 §9, ADR #138 §2).

    ``finished_at`` is aligned with ``statuses``: when each upstream became final, for a failure at the
    decision or its timeout, not when its work failed. ``one_success`` runs from its earliest success,
    ``one_failed`` from its earliest final failure; any other outcome needs every upstream final, so it
    dates from the latest one. None while the rule waits, for an unknown rule, or when no time is known.
    """
    _aligned(statuses, finished_at, "finished_at")
    evaluation = evaluate(rule, statuses, skip_origins)
    if evaluation.verdict in (WAIT, FAIL):
        return None
    if evaluation.verdict == READY and rule in (TriggerRule.ONE_SUCCESS, TriggerRule.ONE_FAILED):
        firing = SUCCESS_ONLY if rule == TriggerRule.ONE_SUCCESS else FINAL_FAILURES
        return min((t for s, t in zip(statuses, finished_at) if s in firing and t is not None), default=None)
    return max((t for t in finished_at if t is not None), default=None)


def _aligned(statuses: Sequence[str], values: Optional[Sequence[object]], name: str) -> Sequence[object]:
    if values is None:
        return [None] * len(statuses)
    if len(values) != len(statuses):
        raise ValueError(f"{name} has {len(values)} entries for {len(statuses)} upstreams")
    return values
