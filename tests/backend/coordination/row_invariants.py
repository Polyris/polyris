"""ADR #137's invariants for tasks as rows, as checks over what the stack holds.

Each check is a pure function over a `RowsView`: the runs and task rows of the
tasks table, the `run_task` executions and their starts, the answers of the
runs' tokens, and the writes and passes the boto3 boundary logged (boundary.py),
in the ADR's own terms. The view that reads them from the stack
comes with the contracts of TR-2, which name the table's fields; the checks do
not change then. `test_row_invariants.py` plants a violation of each and shows
that its check flags it, and a near miss that it does not.

Invariants are staged (ADR #137, *Tests*): the World runs a check from the
stage that builds its mechanism (`STAGE_OF`, stages.py). Invariant 8 is checked
in both directions. `ELSEWHERE` names the invariants another test holds, and
`PENDING` those whose check needs a part of the harness not built yet;
test_meta.py requires every invariant from 1 to 12 in exactly one of the three.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, FrozenSet, List, Optional, Protocol, Tuple

from polyris.constants import TaskStatus
from polyris.coordination.statuses import TERMINAL

#: A row is final once no work for it remains in its run: the terminal statuses and a Stop (ADR #137 §4).
#: TR-2's status set in polyris/coordination replaces this.
FINAL: FrozenSet[str] = TERMINAL | {TaskStatus.STOPPED.value}
#: Statuses in which a task may have an execution of its current generation: started, or running.
MAY_RUN: FrozenSet[str] = frozenset({TaskStatus.DEPS_READY.value, TaskStatus.RUNNING.value})
#: The longest a run row with work left goes without a check (invariant 9).
RUN_CHECK_EVERY = timedelta(minutes=30)
#: Fields a 1.3 task row never carries: its run holds the only token (invariant 1).
TOKEN_FIELDS: Tuple[str, ...] = ("wait_token", "pause_token", "orchestration_token", "wrapper_execution_arn")


@dataclass(frozen=True)
class Task:
    key: str
    run: str
    status: str
    generation: int
    token_fields: Tuple[str, ...] = ()        # which of TOKEN_FIELDS the row carries
    expires_at: Optional[datetime] = None     # the row's TTL
    output_recorded: bool = False             # its output# row exists


@dataclass(frozen=True)
class Run:
    id: str
    status: str                               # creating, active, or how it ended
    token: Optional[str]                      # the current token
    answered: Optional[str] = None            # the token whose answer the run row records
    stop_mark: bool = False
    stop_done: bool = False
    request_pending: bool = False
    paused: bool = False
    due_at: Optional[datetime] = None         # the run row's due time in attention-index
    expires_at: Optional[datetime] = None
    execution_ended: bool = False             # its pipeline execution is no longer RUNNING


@dataclass(frozen=True)
class RunTaskExecution:
    task: str
    generation: int
    running: bool


@dataclass(frozen=True)
class Start:
    """A `run_task` start, with the task row as it stood when the start was sent."""

    task: str
    generation: int
    row_status: str
    row_generation: int


@dataclass(frozen=True)
class RowWrite:
    """A write that changed a task row, in the ADR's terms; the view derives it from the boundary's log.

    `kind`: "decision" (a pass decides the row), "action" (an operator's action through the console),
    "request" (the dispatcher carries out a request), "stop" (the engine's stop of a run),
    "run_task", "start_run", or "other".
    """

    kind: str
    task: str
    status_before: Optional[str]          # None for an insert
    status_after: str
    generation_before: int
    generation_after: int
    conditional: bool = False             # conditioned on the status and the generation it read
    checks_run_version: bool = False      # its transaction checks the run row's run_version
    writer_generation: Optional[int] = None   # run_task's own generation


@dataclass(frozen=True)
class Pass:
    """One pass of the dispatcher, with the reads it could not complete and what it did."""

    id: str
    unresolved_read_failures: int = 0     # reads that failed or came back partial, and were not completed
    starts: int = 0
    closes: int = 0
    answers: int = 0


@dataclass(frozen=True)
class Answer:
    """An answer to a run's token, with the rows that were not final when it was sent."""

    run: str
    token: str
    unfinished: Tuple[str, ...] = ()
    stop_mark: bool = False


class RowsView(Protocol):
    now: datetime

    def runs(self) -> List[Run]: ...

    def tasks(self, run: str) -> List[Task]: ...

    def run_task_executions(self) -> List[RunTaskExecution]: ...

    def starts(self) -> List[Start]: ...

    def answers(self) -> List[Answer]: ...

    def row_writes(self) -> List[RowWrite]: ...

    def passes(self) -> List[Pass]: ...


@dataclass
class View:
    """A `RowsView` held in memory: what the planted tests build, and what a stack's view returns."""

    now: datetime
    run_list: List[Run] = field(default_factory=list)
    task_list: List[Task] = field(default_factory=list)
    executions: List[RunTaskExecution] = field(default_factory=list)
    start_list: List[Start] = field(default_factory=list)
    answer_list: List[Answer] = field(default_factory=list)
    write_list: List[RowWrite] = field(default_factory=list)
    pass_list: List[Pass] = field(default_factory=list)

    def runs(self) -> List[Run]:
        return self.run_list

    def tasks(self, run: str) -> List[Task]:
        return [t for t in self.task_list if t.run == run]

    def run_task_executions(self) -> List[RunTaskExecution]:
        return self.executions

    def starts(self) -> List[Start]:
        return self.start_list

    def answers(self) -> List[Answer]:
        return self.answer_list

    def row_writes(self) -> List[RowWrite]:
        return self.write_list

    def passes(self) -> List[Pass]:
        return self.pass_list


def _all_tasks(view: RowsView) -> List[Task]:
    return [t for run in view.runs() for t in view.tasks(run.id)]


def invariant_1(view: RowsView) -> List[str]:
    """A task that does not run has no execution and no token."""
    found = []
    running = {(e.task, e.generation) for e in view.run_task_executions() if e.running}
    for task in _all_tasks(view):
        if task.token_fields:
            found.append(f"invariant 1: {task.key} carries {', '.join(task.token_fields)}")
        if task.status not in MAY_RUN and (task.key, task.generation) in running:
            found.append(f"invariant 1: {task.key} is {task.status}, yet run_task runs for its generation {task.generation}")
    return found


#: Statuses only the dispatcher, or an operator, moves a row out of (invariant 2).
WAITING_STATES: FrozenSet[str] = frozenset({TaskStatus.WAITING.value, TaskStatus.WAITING_PAUSED.value})
#: Kinds of write that end a task's ownership, and so raise its generation (invariant 6).
OWNERSHIP_ENDS: FrozenSet[str] = frozenset({"action", "request", "stop"})


def invariant_2(view: RowsView) -> List[str]:
    """Apart from an operator's action, only the dispatcher moves a row out of waiting or waiting_paused,
    with a decision conditioned on the status and generation it read and on the run row's run_version."""
    found = []
    for w in view.row_writes():
        if w.status_before not in WAITING_STATES or w.status_after == w.status_before:
            continue
        if w.kind not in {"decision"} | OWNERSHIP_ENDS:
            found.append(f"invariant 2: {w.task} left {w.status_before} for {w.status_after} by a {w.kind} write")
        elif w.kind == "decision" and not (w.conditional and w.checks_run_version):
            found.append(f"invariant 2: a decision moved {w.task} to {w.status_after} without its conditions")
    return found


def invariant_3(view: RowsView) -> List[str]:
    """Write first, then start: run_task starts only for a row already deps_ready under that generation."""
    return [f"invariant 3: run_task started for {s.task} generation {s.generation} while the row was "
            f"{s.row_status} under generation {s.row_generation}"
            for s in view.starts()
            if s.row_status != TaskStatus.DEPS_READY.value or s.row_generation != s.generation]


def invariant_4(view: RowsView) -> List[str]:
    """At most one run_task per row and generation."""
    counts: Dict[Tuple[str, int], int] = {}
    for e in view.run_task_executions():
        counts[(e.task, e.generation)] = counts.get((e.task, e.generation), 0) + 1
    return [f"invariant 4: {task} has {n} run_task executions for generation {gen}"
            for (task, gen), n in sorted(counts.items()) if n > 1]


def invariant_5(view: RowsView) -> List[str]:
    """run_task writes only under its own generation, and never changes it."""
    return [f"invariant 5: run_task of generation {w.writer_generation} wrote {w.task} under generation "
            f"{w.generation_before}" + ("" if w.generation_after == w.generation_before else f", making it {w.generation_after}")
            for w in view.row_writes()
            if w.kind == "run_task" and (w.writer_generation != w.generation_before or w.generation_after != w.generation_before)]


def invariant_6(view: RowsView) -> List[str]:
    """An action that ends a task's ownership raises its generation by one, in the same write."""
    return [f"invariant 6: a {w.kind} moved {w.task} from {w.status_before} to {w.status_after} "
            f"with generation {w.generation_before} -> {w.generation_after}"
            for w in view.row_writes()
            if w.kind in OWNERSHIP_ENDS and w.status_after != w.status_before
            and w.generation_after != w.generation_before + 1]


def invariant_8(view: RowsView) -> List[str]:
    """A run's end is decided from facts, in both directions.

    When every row is final, the current token is answered and the run row records it, or the run row
    stays due so that a later check answers it. No token is answered, and no run's execution ends, while
    a row is not final, outside the stop paths (§4).
    """
    found = []
    for run in view.runs():
        if run.status == "creating":
            continue
        tasks = view.tasks(run.id)
        unfinished = sorted(t.key for t in tasks if t.status not in FINAL)
        if not unfinished and run.token and run.answered != run.token and not run.execution_ended and run.due_at is None:
            found.append(f"invariant 8: every row of {run.id} is final, its token is not answered, and nothing will check it")
        if unfinished and run.execution_ended and not run.stop_mark:
            found.append(f"invariant 8: the execution of {run.id} ended while {', '.join(unfinished)} were not final")
    for answer in view.answers():
        if answer.unfinished and not answer.stop_mark:
            found.append(f"invariant 8: the token of {answer.run} was answered while "
                         f"{', '.join(answer.unfinished)} were not final")
    return found


def invariant_10(view: RowsView) -> List[str]:
    """No fail-open: a pass with a read it could not complete starts, closes and answers nothing."""
    return [f"invariant 10: pass {p.id} could not complete {p.unresolved_read_failures} read(s), yet made "
            f"{p.starts} start(s), {p.closes} close(s) and {p.answers} answer(s)"
            for p in view.passes() if p.unresolved_read_failures and (p.starts or p.closes or p.answers)]


def invariant_9(view: RowsView) -> List[str]:
    """Every unfinished row is covered by its run row's due time, at most 30 minutes ahead."""
    found = []
    for run in view.runs():
        tasks = view.tasks(run.id)
        work_left = (any(t.status not in FINAL for t in tasks) or (run.stop_mark and not run.stop_done)
                     or run.request_pending or (run.token is not None and run.answered != run.token
                                                and not run.execution_ended))
        if run.status == "creating" or not work_left:
            continue
        if run.due_at is None:
            found.append(f"invariant 9: {run.id} has work left and no due time")
        elif run.due_at > view.now + RUN_CHECK_EVERY:
            found.append(f"invariant 9: {run.id} is next checked at {run.due_at.isoformat()}, "
                         f"more than {RUN_CHECK_EVERY} ahead")
    return found


def invariant_11(view: RowsView) -> List[str]:
    """A row outlives its run: no row of a run that has not ended expires."""
    found = []
    for run in view.runs():
        if run.execution_ended:
            continue
        if run.expires_at is not None and run.expires_at <= view.now:
            found.append(f"invariant 11: the run row of {run.id} expired while the run goes on")
        for task in view.tasks(run.id):
            if task.expires_at is not None and task.expires_at <= view.now:
                found.append(f"invariant 11: {task.key} expired while its run {run.id} goes on")
    return found


def invariant_12(view: RowsView) -> List[str]:
    """No status is visible before the data its dependants read: a success comes with its output."""
    return [f"invariant 12: {t.key} reads success, but its output is not recorded"
            for t in _all_tasks(view) if t.status == TaskStatus.SUCCESS.value and not t.output_recorded]


def liveness(view: RowsView) -> List[str]:
    """At a scenario's end, once the clock passed every deadline and the sweeper found nothing more to do:
    every run has ended or is paused, or a row waits for a decision with a due time."""
    found = []
    for run in view.runs():
        if run.execution_ended or run.paused:
            continue
        deciding = [t for t in view.tasks(run.id) if t.status == TaskStatus.WAITING_DECISION.value]
        if not (deciding and run.due_at is not None):
            found.append(f"liveness: {run.id} neither ended nor is paused, and no row waits for a decision")
    return found


CHECKS: Dict[int, Callable[[RowsView], List[str]]] = {
    1: invariant_1, 2: invariant_2, 3: invariant_3, 4: invariant_4, 5: invariant_5, 6: invariant_6,
    8: invariant_8, 9: invariant_9, 10: invariant_10, 11: invariant_11, 12: invariant_12,
}

#: The stage from which the World runs each check (stages.py, ADR #137 *Tests*).
STAGE_OF: Dict[int, str] = {1: "rows-core", 2: "rows-core", 3: "rows-core", 4: "rows-core", 5: "rows-run-task",
                            6: "rows-ops", 8: "rows-core", 9: "rows-core", 10: "rows-core", 11: "rows-core",
                            12: "rows-run-task"}

#: Invariants another test holds, and where.
ELSEWHERE: Dict[int, str] = {
    7: "tests/sdk/test_task_row_writers.py: every writer of a task row against the transition table (TR-2)",
}

#: Invariants whose check needs a part of the harness that is not built yet.
PENDING: Dict[int, str] = {}
