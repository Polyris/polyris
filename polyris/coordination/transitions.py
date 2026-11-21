"""Which writer may move a task row or a run row, and which moves wake the dispatcher (ADR #137 §3, §7).

This is the table of invariant 7. A writer takes the condition of its write from
it (``TaskTransition.sources``); a decision's write also checks the row's
generation and the run row's ``run_version`` (§1). The stream's filter patterns
come from its *Wakes* column (``filter_patterns``), and the dispatcher drops what
the filters let through and the table does not mark (``wakes``). A task row's
kind in ``attention-index`` follows from its status (``attention_kind``, §6).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple

from ..constants import FAILURE_STATUSES, SETTLED_STATUSES, SUCCESS_STATUSES, SkipOrigin, TaskStatus
from . import attention, ownership, run_row
from .statuses import RECONCILABLE

S = TaskStatus


class Writer:
    """Who writes a row (§3)."""
    START_RUN = "start_run"
    DISPATCHER = "dispatcher"
    RUN_TASK = "run_task"
    CONSOLE = "console"
    STEP = "step"  # a direct step's branch, which marks its failure on the run row


WRITERS = frozenset(v for k, v in vars(Writer).items() if not k.startswith("_"))
#: Both kinds of row keep their status in this field.
STATUS = "status"
#: The source of a write that creates a row; for the run row also a row that a direct step's mark made alone.
NO_ROW: Optional[str] = None

#: Final: a row that no longer changes on its own. ``succeeded`` and ``aborted`` occur only on rows from before 1.3.
FINAL: FrozenSet[str] = frozenset(SETTLED_STATUSES)
#: Every status that is not final, those of rows from before 1.3 included.
NOT_FINAL: FrozenSet[str] = frozenset(s.value for s in S) - FINAL
#: The final failures, which a Skip or a Mark Successful turns into an outcome that lets dependants run.
FINAL_FAILURES: FrozenSet[str] = frozenset(FAILURE_STATUSES | {S.STOPPED.value})
_WAITS = RECONCILABLE
_NO_OUTCOME_YET = frozenset(s.value for s in S) - SUCCESS_STATUSES


@dataclass(frozen=True)
class TaskTransition:
    """One move of a task row that the table allows."""
    name: str
    sources: FrozenSet[Optional[str]]
    target: str
    writers: FrozenSet[str]
    wakes: bool
    skip_origin: Optional[str] = None
    #: An action that ends the row's ownership raises its generation in the same write (§3, invariant 6): every
    #: write of the console, and the dispatcher's for a stop or a request. A writer takes it from here.
    raises_generation: bool = False


@dataclass(frozen=True)
class RunTransition:
    """One write of the run row that the table allows; ``target`` None keeps the status."""
    name: str
    sources: FrozenSet[Optional[str]]
    target: Optional[str]
    writers: FrozenSet[str]
    raises_version: bool
    wakes: bool


def _task(name: str, sources: Iterable[Optional[str]], target: str, writers: Iterable[str], wakes: bool,
          skip_origin: Optional[str] = None, *, raises: bool = False) -> TaskTransition:
    return TaskTransition(name, frozenset(sources), target, frozenset(writers), wakes, skip_origin, raises)


def _run(name: str, sources: Iterable[Optional[str]], target: Optional[str], writers: Iterable[str],
         raises_version: bool, wakes: bool) -> RunTransition:
    return RunTransition(name, frozenset(sources), target, frozenset(writers), raises_version, wakes)


_D, _R, _C, _SR = Writer.DISPATCHER, Writer.RUN_TASK, Writer.CONSOLE, Writer.START_RUN
_CREATING, _ACTIVE = run_row.CREATING, run_row.ACTIVE

TASK_TRANSITIONS: Tuple[TaskTransition, ...] = (
    _task("create", {NO_ROW}, S.WAITING.value, {_SR}, False),
    _task("create_skipped", {NO_ROW}, S.SKIPPED.value, {_SR}, False, SkipOrigin.RUN),  # named in skip_tasks
    _task("hold", {S.WAITING.value}, S.WAITING_PAUSED.value, {_D}, False),
    _task("release", {S.WAITING_PAUSED.value}, S.WAITING.value, {_D}, False),
    _task("ready", _WAITS, S.DEPS_READY.value, {_D}, False),                 # written before run_task starts
    _task("skip_by_rule", _WAITS, S.SKIPPED.value, {_D}, True, SkipOrigin.RULE),
    _task("block", _WAITS, S.UPSTREAM_FAILED.value, {_D}, True),
    _task("fail_wait", _WAITS, S.FAILED.value, {_D}, True),       # its limit, a CoordinationError, an upstream row gone
    _task("fail_start", {S.DEPS_READY.value}, S.FAILED.value, {_D}, True),   # refused for good, or failing past its deadline
    _task("run", {S.DEPS_READY.value}, S.RUNNING.value, {_R}, False),
    _task("succeed", {S.RUNNING.value}, S.SUCCESS.value, {_R}, True),
    _task("ask", {S.RUNNING.value}, S.WAITING_DECISION.value, {_R}, True),
    _task("fail_dead", {S.DEPS_READY.value, S.RUNNING.value}, S.FAILED.value, {_D}, True),  # execution ended, no outcome
    _task("fail_undecided", {S.WAITING_DECISION.value}, S.FAILED.value, {_D}, True),      # the decision timeout
    _task("skip", _NO_OUTCOME_YET, S.SKIPPED.value, {_C}, True, SkipOrigin.MANUAL, raises=True),
    _task("mark_success", _NO_OUTCOME_YET, S.SUCCESS.value, {_C}, True, raises=True),
    _task("fail", NOT_FINAL, S.FAILED.value, {_C}, True, raises=True),
    _task("stop", NOT_FINAL, S.STOPPED.value, {_C, _D}, True, raises=True),    # dispatcher: a Stop of the run, a run row gone
    _task("restart", FINAL | {S.WAITING_DECISION.value}, S.WAITING.value, {_C}, True, raises=True),
    # Restart with dependants: the descendants that a failure or a Stop decided, upstream_failed or skipped by a
    # rule (the dispatcher builds the set from its reads; an operator's or a run's skip stays).
    _task("reset", {S.UPSTREAM_FAILED.value, S.SKIPPED.value}, S.WAITING.value, {_D}, True, raises=True),
)

RUN_TRANSITIONS: Tuple[RunTransition, ...] = (
    _run("create", {NO_ROW}, _CREATING, {_SR}, True, False),                 # with the attempt's token
    _run("mark_step_failure", {NO_ROW, _CREATING, _ACTIVE}, None, {Writer.STEP}, False, False),
    _run("activate", {_CREATING}, _ACTIVE, {_SR}, True, True),               # after the last task row, unless stopped
    _run("new_token", {_CREATING, _ACTIVE}, None, {_SR}, True, False),       # a later attempt, or a redrive
    _run("stop", {_ACTIVE}, None, {_C}, True, True),                         # the console's Stop of a run
    _run("engine_stop", {_CREATING, _ACTIVE}, None, {_D}, True, True),       # its execution ended, or it never became active
    _run("request", {_ACTIVE}, None, {_C}, True, True),                      # Restart, Restart with dependants
    _run("task_action", {_ACTIVE}, None, {_C}, True, False),                 # Skip, Fail, Mark Successful, Stop of a task
    _run("reset", {_ACTIVE}, None, {_D}, True, False),                       # each reset of a Restart with dependants
    _run("pause", {_ACTIVE}, None, {_C}, True, True),
    _run("resume", {_ACTIVE}, None, {_C}, True, True),
    _run("clear_request", {_ACTIVE}, None, {_D}, True, False),
    _run("answer", {_CREATING, _ACTIVE}, None, {_D}, True, False),           # every row final: the token answered
    _run("stop_done", {_CREATING, _ACTIVE}, None, {_D}, True, False),
)

#: The (old status, new status) pairs of a task row whose change wakes the dispatcher.
WAKING: FrozenSet[Tuple[Optional[str], str]] = frozenset(
    (source, t.target) for t in TASK_TRANSITIONS if t.wakes for source in t.sources)

#: A task row's kind in ``attention-index``, by status (§6). A ``waiting`` row has the entry only while the
#: due-time function gives it a time of its own: a limit outside a pause, or a countdown (invariant 9).
ATTENTION: Mapping[str, str] = {
    S.WAITING.value: attention.WAITING,
    S.DEPS_READY.value: attention.WAITING,
    S.RUNNING.value: attention.WAITING,
    S.WAITING_DECISION.value: attention.DECISION,
    S.FAILED.value: attention.FAILED,
}


def task_transition(name: str) -> TaskTransition:
    """The task-row transition called ``name``; a writer builds the condition of its write from its sources."""
    for t in TASK_TRANSITIONS:
        if t.name == name:
            return t
    raise ValueError(f"unknown task-row transition {name!r}; expected one of {sorted(t.name for t in TASK_TRANSITIONS)}")


def run_transition(name: str) -> RunTransition:
    """The run-row transition called ``name``."""
    for t in RUN_TRANSITIONS:
        if t.name == name:
            return t
    raise ValueError(f"unknown run-row transition {name!r}; expected one of {sorted(t.name for t in RUN_TRANSITIONS)}")


def allowed(source: Optional[str], target: str, writer: str) -> bool:
    """Whether the table lets ``writer`` move a task row from ``source`` (None: no row) to ``target``."""
    return any(source in t.sources and t.target == target and writer in t.writers for t in TASK_TRANSITIONS)


def attention_kind(status: str) -> Optional[str]:
    """The kind of a task row's entry in ``attention-index`` while it holds ``status``, if it has one."""
    return ATTENTION.get(status)


def task_row_wakes(old: Optional[Mapping[str, Any]], new: Optional[Mapping[str, Any]]) -> bool:
    """Whether this change of a task row wakes the dispatcher: a waking transition of the table.

    An insert, a deletion, a write that keeps the status (a due time, a TTL, a
    ``waiting_decision`` row rewritten in a pause) and a row without a
    generation (``output#``, a warning) wake nothing.
    """
    if old is None or new is None or ownership.FIELD not in new:
        return False
    return (old.get(STATUS), new.get(STATUS)) in WAKING


def run_row_wakes(old: Optional[Mapping[str, Any]], new: Optional[Mapping[str, Any]]) -> bool:
    """Whether this change of a run row wakes the dispatcher (§7).

    It does when the run became active, a stop mark or a request arrived, the
    pause opened or closed, or a pass set ``left_at``. Its insert does not: a
    pass over a ``creating`` run would return at once. Neither do a new token,
    the answer, a cleared request, the stop done, a summary, a due time, or the
    sweeper's clearing of ``left_at``.
    """
    if old is None or new is None:
        return False
    old_requests = old.get(run_row.REQUESTS) or {}
    return ((old.get(run_row.STATUS) == _CREATING and new.get(run_row.STATUS) == _ACTIVE)
            or (run_row.STOP in new and run_row.STOP not in old)
            or old.get(run_row.PAUSES) != new.get(run_row.PAUSES)
            or any(old_requests.get(kind) != request for kind, request in (new.get(run_row.REQUESTS) or {}).items())
            or new.get(run_row.LEFT_AT) not in (None, old.get(run_row.LEFT_AT)))


_TYPE_TAGS = frozenset({"S", "N", "B", "BOOL", "NULL", "M", "L", "SS", "NS", "BS"})


def wakes(row_key: str, old: Optional[Mapping[str, Any]], new: Optional[Mapping[str, Any]]) -> bool:
    """Whether a stream record of the row ``row_key`` wakes the dispatcher; the dispatcher drops the others.

    ``old`` and ``new`` are the record's images deserialized (``TypeDeserializer``): on the raw images a
    status would compare unequal to every name, and a lost wake-up would pass silently.
    """
    for image in (old, new):
        if image and any(isinstance(v, dict) and len(v) == 1 and next(iter(v)) in _TYPE_TAGS
                         for v in image.values()):
            raise ValueError("deserialize the stream record's images before asking whether they wake")
    if run_row.is_run_row(row_key):
        return run_row_wakes(old, new)
    return task_row_wakes(old, new)


def filter_patterns() -> List[Dict[str, Any]]:
    """The ``FilterCriteria`` patterns of the tasks table's event source mapping (§7).

    Three are for task rows, and each requires the field only task rows carry,
    since ``output#`` and warning rows have a status too. A pattern says
    "became" through the old image. The fourth lets every insert and change of
    a run row through, because a filter cannot compare a field's old and new
    values; ``wakes`` drops what the table does not mark. Four of the five
    filters Lambda allows per mapping by default.
    """
    def task_rows(new: List[Any], old: List[Any]) -> Dict[str, Any]:
        return {"dynamodb": {"NewImage": {ownership.FIELD: {"N": [{"exists": True}]}, STATUS: {"S": new}},
                             "OldImage": {STATUS: {"S": old}}}}

    final_or_decision = sorted(FINAL | {S.WAITING_DECISION.value})
    return [
        task_rows(final_or_decision, [{"anything-but": sorted(FINAL)}]),                    # became final, or asks
        task_rows([S.WAITING.value], final_or_decision),                                    # restarted
        task_rows(sorted({S.SUCCESS.value, S.SKIPPED.value}), sorted(FINAL_FAILURES)),       # a final failure resolved
        {"eventName": ["INSERT", "MODIFY"],
         "dynamodb": {"Keys": {run_row.TABLE_KEY: {"S": [{"prefix": run_row.KEY_PREFIX}]}}}},
    ]
