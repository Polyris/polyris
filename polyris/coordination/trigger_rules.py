"""Trigger-rule evaluation over dependency statuses (ADR #115, #117).

The single implementation the reconciler uses (ADR #130 §3).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .statuses import FAILURE, SKIPPED, SUCCESS_ONLY, TERMINAL

KNOWN_RULES = frozenset({"all_success", "one_success", "all_done", "all_skipped", "none_skipped"})

#: Names removed by ADR #117, mapped to the rule they always behaved as.
ALIASES = {
    "one_done": "all_done",
    "none_failed": "all_done",
    "none_failed_min_one_success": "one_success",
}

#: Rules that require success or absence of failure. When one of them can no
#: longer be satisfied and a real failure is present, the task is
#: ``upstream_failed``; otherwise the rule's condition simply never occurred
#: and the task is skipped (ADR #115).
FAILURE_AVERSE_RULES = frozenset({"all_success", "one_success"})

READY = "ready"
WAIT = "wait"
SKIP = "skip"
UPSTREAM_FAILED = "upstream_failed"


@dataclass(frozen=True)
class Counts:
    total: int
    success: int
    failed: int
    skipped: int
    pending: int


@dataclass(frozen=True)
class DepEvaluation:
    """Outcome of a trigger rule over the dependencies' current statuses."""

    verdict: str
    satisfied: bool
    reason: str
    #: The rule actually evaluated: aliases resolved, unknown names → all_success.
    effective_rule: str
    #: False when the stored rule name is not a known rule or alias.
    rule_known: bool
    counts: Counts


def count(statuses: Sequence[str]) -> Counts:
    """Count dependency statuses the way every trigger rule reads them."""
    return Counts(
        total=len(statuses),
        success=sum(1 for s in statuses if s in SUCCESS_ONLY),
        failed=sum(1 for s in statuses if s in FAILURE),
        skipped=sum(1 for s in statuses if s == SKIPPED),
        pending=sum(1 for s in statuses if s not in TERMINAL),
    )


def evaluate(
    rule: str,
    statuses: Sequence[str],
    rule_originated_skip: Optional[Sequence[bool]] = None,
) -> DepEvaluation:
    """Evaluate ``rule`` over ``statuses`` and classify the result.

    ``rule_originated_skip`` is aligned with ``statuses``: True where that
    dependency is ``skipped`` because its own trigger rule never fired. Only
    ``all_success`` reads it — such a skip cascades instead of counting as ok
    (ADR #115). ``None`` means no skip is rule-originated.
    """
    if rule_originated_skip is not None and len(rule_originated_skip) != len(statuses):
        raise ValueError(
            f"rule_originated_skip has {len(rule_originated_skip)} entries "
            f"for {len(statuses)} dependencies"
        )
    rule_known = rule in KNOWN_RULES or rule in ALIASES
    effective_rule = ALIASES.get(rule, rule) if rule_known else "all_success"
    counts = count(statuses)
    satisfied, reason = _check(effective_rule, counts, rule_originated_skip or ())

    blocked = counts.pending == 0 and not satisfied
    if not blocked:
        verdict = READY if satisfied else WAIT
    elif effective_rule in FAILURE_AVERSE_RULES and counts.failed > 0:
        verdict = UPSTREAM_FAILED
    else:
        verdict = SKIP
    if not rule_known:
        reason = f"unknown trigger_rule {rule!r}, evaluated as all_success: {reason}"
    return DepEvaluation(verdict, satisfied, reason, effective_rule, rule_known, counts)


def _check(rule: str, c: Counts, rule_originated_skip: Sequence[bool]) -> tuple[bool, str]:
    if c.total == 0:
        return True, "no dependencies"
    if rule == "all_success":
        cascading = sum(1 for flag in rule_originated_skip if flag)
        ok = c.success + c.skipped - cascading
        if c.pending == 0 and ok == c.total:
            return True, "all_success"
        if c.pending > 0:
            return False, f"waiting for {c.pending} dependencies"
        if c.failed > 0:
            return False, f"{c.failed} dependencies failed"
        return False, f"{cascading} dependencies skipped by their own rule"
    if rule == "one_success":
        if c.success >= 1:
            return True, "one_success"
        return False, "waiting, no success yet" if c.pending > 0 else "no dependency succeeded"
    if rule == "all_done":
        if c.pending == 0:
            return True, "all_done"
        return False, f"waiting for {c.pending} dependencies"
    if rule == "all_skipped":
        if c.pending == 0 and c.skipped == c.total:
            return True, "all_skipped"
        if c.pending > 0:
            return False, f"waiting for {c.pending} dependencies"
        return False, f"not all skipped ({c.skipped}/{c.total})"
    if rule == "none_skipped":
        if c.skipped > 0:
            return False, f"{c.skipped} dependencies skipped"
        if c.pending == 0:
            return True, "none_skipped"
        return False, f"waiting for {c.pending} dependencies"
    # evaluate() maps every name to a member of KNOWN_RULES first; this guards
    # a future rule added to KNOWN_RULES without a branch here.
    raise ValueError(f"unhandled trigger rule {rule!r}")  # pragma: no cover - unreachable through evaluate()
