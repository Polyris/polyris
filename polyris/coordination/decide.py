"""The readiness decision for one waiting task (ADR #130 §3).

A pure function of facts a pass of the dispatcher has already read. It never
touches AWS; the pass turns the verdict into a write (``dispatch``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from . import trigger_rules
from .asset_conditions import Evaluation
from .statuses import RECONCILABLE

NOOP = "noop"
SKIP = trigger_rules.SKIP
UPSTREAM_FAILED = trigger_rules.UPSTREAM_FAILED
WAIT = trigger_rules.WAIT
PAUSED = "paused"
READY = trigger_rules.READY
#: A rule the engine does not know: the task fails with the cause (ADR #138 §2).
FAIL = trigger_rules.FAIL

VERDICTS = frozenset({NOOP, SKIP, UPSTREAM_FAILED, WAIT, PAUSED, READY, FAIL})


@dataclass(frozen=True)
class TaskView:
    """The fields of a task row the decision reads."""

    status: str
    trigger_rule: str
    dependencies: Tuple[str, ...]
    has_asset_conditions: bool


@dataclass(frozen=True)
class Decision:
    verdict: str
    reasons: Tuple[str, ...]
    deps: Optional[trigger_rules.DepEvaluation] = None
    assets: Optional[Evaluation] = None


def decide(
    task: Optional[TaskView],
    *,
    dep_statuses: Sequence[str] = (),
    skip_origins: Optional[Sequence[Optional[str]]] = None,
    assets: Optional[Evaluation] = None,
    paused: bool = False,
) -> Decision:
    """Decide what to do with ``task``.

    ``dep_statuses`` is aligned with ``task.dependencies``, and so is
    ``skip_origins``: the ``skip_origin`` of a skipped dependency. ``assets`` may be
    omitted when the task has no asset conditions, or when the dependencies
    already decide the outcome (pending, or never satisfiable): a pass of the
    dispatcher skips the asset reads in those cases.
    """
    if task is None:
        return Decision(NOOP, ("task row not found",))
    if task.status not in RECONCILABLE:
        return Decision(NOOP, (f"status {task.status!r} is not a waiting status",))
    if len(dep_statuses) != len(task.dependencies):
        raise ValueError(
            f"{len(dep_statuses)} dependency statuses for {len(task.dependencies)} dependencies"
        )

    evaluation = trigger_rules.evaluate(task.trigger_rule, dep_statuses, skip_origins)
    if evaluation.verdict in (SKIP, UPSTREAM_FAILED, FAIL):
        # No asset gate: a task whose dependencies can never satisfy its rule can never run, whatever
        # its assets do (ADR #122, #130), and an unknown rule fails its task, a root task included.
        return Decision(evaluation.verdict, (evaluation.reason,), evaluation, assets)
    reasons: list[str] = [evaluation.reason] if evaluation.verdict == WAIT else []
    deps = evaluation if task.dependencies else None

    if task.has_asset_conditions:
        if assets is None:
            if reasons:
                return Decision(WAIT, tuple(reasons), deps)
            raise ValueError("the task has asset conditions but they were not evaluated")
        if not assets.ready:
            # Only when the spec as a whole fails: members of a satisfied OR
            # group may be unready, and that must not hold the task back.
            reasons.extend(f"{a.asset_name}: {a.reason}" for a in assets.assets if not a.ready)

    if reasons:
        return Decision(WAIT, tuple(reasons), deps, assets)
    if paused:
        return Decision(PAUSED, ("pipeline is paused",), deps, assets)
    return Decision(READY, ("all conditions met",), deps, assets)
