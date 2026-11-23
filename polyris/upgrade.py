"""The move of a platform from 1.2 to 1.3, as ``polyris-up`` makes it (ADR #137, *Migration and upgrade*).

1.3 runs tasks as rows and deletes 1.2's engine, and nothing adopts a 1.2 run.
Before the update, ``check`` refuses a platform that is older than 1.2.0, whose
1.2 has not finished adopting older rows and facts for 48 hours, or that has a
run in flight. Once 1.3 is installed, ``stop_leftovers`` stops what 1.2 left
running, and ``wait_for_index`` waits for the index that the console's run
lists read.

The rules are pure and live in ``polyris/platform.py``. This module is their
AWS side: it reads and stops through the boto3 clients its caller passes, and
says when it waits. ``polyris/up.py`` decides when each step runs and prints
what it returns or raises.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Set, Tuple

from botocore.exceptions import ClientError

from ._ansi import CYAN, YELLOW
from ._ansi import c as _c
from .constants import ACCESS_DENIED_CODES
from .coordination import run_row, sweep
from .platform import (
    ADOPTION_SETTLE,
    BULK_BACKFILL_MACHINE,
    OLDEST_UPGRADABLE,
    OUTPUT_SPECS_TABLE,
    OUTPUT_TOKENS_TABLE,
    PARAM_NAMESPACE,
    PARAM_STAGE,
    REMOVED_STANDARD_MACHINES,
    RUN_TASK_MACHINE,
    adoption_finished_at,
    outputs_of,
    parameters_of,
    platform_prefix,
    release_series,
    state_machine_arn,
)

#: The flag that upgrades a platform whose 1.2 has not settled or that has a run in flight.
#: It never covers a platform older than 1.2.0.
OVERRIDE_FLAG = "--accept-stopped-runs"
#: The engine's Standard state machines of 1.2: a running execution of one is a run in flight.
#: ListExecutions lists no Express execution, and the three test state machines are a task's work.
ENGINE_MACHINES = REMOVED_STANDARD_MACHINES + (BULK_BACKFILL_MACHINE, RUN_TASK_MACHINE)
#: How many running executions of one state machine the check counts before it says "or more".
_COUNT_UP_TO = 100
#: What the history of an execution that polyris-up stopped says.
STOP_ERROR = "Polyris.Upgrade"
STOP_CAUSE = ("polyris-up stopped this execution: the platform runs 1.3 now, which adopts no run that 1.2 "
              "started. Start the run again after its pipeline's redeploy.")
#: How long the stop waits before it lists again: ListExecutions shows a new execution only after a moment.
STOP_SETTLE_S = 2.0
#: How often the wait for the index asks DynamoDB, and how long it waits at most: as long as for a stack.
INDEX_POLL_S = 15.0
INDEX_WAIT_S = 3600.0


class Refused(Exception):
    """A check refused the upgrade: the reason, then what to do about it."""

    def __init__(self, reason: str, *todo: str):
        super().__init__(reason)
        self.todo = todo


@dataclass(frozen=True)
class Target:
    """The deployed platform a step acts on: its stack, where its state machines are, and its tables."""

    stack_name: str
    #: What the names of its state machines, and of its pipelines', start with: ``{Namespace}-{Stage}-polyris-``.
    name_prefix: str
    #: The ARN of any of its state machines, up to the end of that prefix.
    machine_prefix: str
    #: The tasks table (``pipeline-tokens``); empty when the stack names none.
    tokens_table: str
    #: ``pipeline-specs``, which the 1.3 update adds; empty when the stack names none.
    specs_table: str

    @classmethod
    def of(cls, stack: Mapping[str, Any], *, partition: str, region: str, account: str) -> "Target":
        """The target a describe_stacks entry describes, in this account and region."""
        params, outputs = parameters_of(stack), outputs_of(stack)
        prefix = platform_prefix(params.get(PARAM_NAMESPACE, ""), params.get(PARAM_STAGE, ""))
        return cls(
            stack_name=str(stack.get("StackName", "")),
            name_prefix=prefix,
            machine_prefix=state_machine_arn(partition=partition, region=region, account=account, name=prefix),
            tokens_table=outputs.get(OUTPUT_TOKENS_TABLE, ""),
            specs_table=outputs.get(OUTPUT_SPECS_TABLE, ""),
        )

    def machine(self, name: str) -> str:
        """The ARN of the platform's state machine whose name ends in ``name``."""
        return self.machine_prefix + name


@contextmanager
def permission(action: str, resource: str) -> Iterator[None]:
    """An access-denied error of the call inside refuses the upgrade and names the permission.

    The checks run before anything changes, so a role that lacks a permission
    learns it here, not halfway through the upgrade.
    """
    try:
        yield
    except ClientError as e:
        if _code(e) not in ACCESS_DENIED_CODES:
            raise
        raise Refused(
            f"polyris-up may not call {action} on {resource}, which its checks before the upgrade to 1.3 need.",
            "Grant it to the role that runs polyris-up (docs/deployment/IAM_PERMISSIONS.md), then run it again.",
        ) from e


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def check(*, sfn: Any, dynamodb: Any, target: Target, deployed: Optional[str], override: bool, now: datetime) -> None:
    """Check a platform below 1.3 before its upgrade; raises ``Refused`` with the reason and what to do.

    It refuses a platform:

    - older than 1.2.0. ``deployed`` is the version in the stack's description
      (ADR #131). A platform deployed with ``sam deploy`` carries none (None),
      and then the ``_engine_health`` row, which only 1.2 writes, tells 1.2
      from 1.1.4;
    - whose 1.2 has not finished adopting older rows and facts, or finished
      less than 48 hours ago, as that row records it;
    - with a run in flight: a running execution of one of the engine's
      Standard state machines.

    ``override`` skips the last two checks, never the first.
    """
    stack = target.stack_name
    not_covered = (f"{OVERRIDE_FLAG} does not cover this check.",) if override else ()
    first_1_2 = ("Upgrade it to 1.2 first: install the last 1.2 release and run its polyris-up "
                 "(CHANGELOG.md, Upgrading to 1.3.0).")
    series = release_series(deployed)
    if series is not None and series < OLDEST_UPGRADABLE:
        raise Refused(f"{stack} runs v{deployed}, and 1.3 upgrades a platform from 1.2 only.", first_1_2, *not_covered)
    health: Optional[Dict[str, str]] = None
    if series is None or not override:
        health = _engine_health(dynamodb, target.tokens_table)
    if series is None and health is None:
        raise Refused(
            f"{stack} looks older than 1.2.0, and 1.3 upgrades a platform from 1.2 only: its stack names no "
            f"version, and its tasks table has no {sweep.HEALTH_KEY} row, which 1.2 writes within 5 minutes "
            "of its install.", first_1_2, *not_covered)
    if override:
        return
    _check_adoption(health, stack=stack, now=now)
    _check_drained(sfn, target)


def _engine_health(dynamodb: Any, table: str) -> Optional[Dict[str, str]]:
    """The sweeper's ``_engine_health`` row with its values as text; None when there is none."""
    if not table:
        return None
    with permission("dynamodb:GetItem", table):
        item = dynamodb.get_item(TableName=table, Key={run_row.TABLE_KEY: {"S": sweep.HEALTH_KEY}}).get("Item")
    if item is None:
        return None
    return {name: str(value.get("S") or value.get("N") or "") for name, value in item.items()}


def _check_adoption(health: Optional[Mapping[str, str]], *, stack: str, now: datetime) -> None:
    anyway = (f"To upgrade now, pass {_c(OVERRIDE_FLAG, CYAN)}: what 1.2 has not adopted is left behind, "
              "and a task that waits for such a fact does not see it.")
    finished = adoption_finished_at(health, now=now)
    if finished is None:
        raise Refused(
            f"1.2 has not finished adopting the rows and facts that older versions wrote on {stack}: its "
            f"{sweep.HEALTH_KEY} row records no end.",
            "Until it has, they need the compatibility code that 1.3 removes. 1.2's sweeper adopts them from its "
            "first run; run polyris-up again 48 hours after it has finished.", anyway)
    if now - finished < ADOPTION_SETTLE:
        hours = int((now - finished).total_seconds() // 3600)
        raise Refused(
            f"1.2 finished adopting older rows and facts on {stack} {hours} h ago. 1.3 can be installed 48 hours "
            f"after that, from {(finished + ADOPTION_SETTLE).strftime('%Y-%m-%d %H:%M')} UTC.",
            "Until then they still need the compatibility code that 1.3 removes.", anyway)


def _check_drained(sfn: Any, target: Target) -> None:
    running: List[str] = []
    for name in ENGINE_MACHINES:
        arn = target.machine(name)
        try:
            with permission("states:ListExecutions", arn):
                page = sfn.list_executions(stateMachineArn=arn, statusFilter="RUNNING", maxResults=_COUNT_UP_TO)
        except ClientError as e:
            if _code(e) != "StateMachineDoesNotExist":
                raise
            continue                                       # a state machine that is not there runs nothing
        count = len(page.get("executions", []))
        if count:
            running.append(f"{count}{' or more' if page.get('nextToken') else ''} of {arn.rsplit(':', 1)[-1]}")
    if running:
        raise Refused(
            f"A run is in flight on {target.stack_name}: executions are running ({', '.join(running)}).",
            "Nothing adopts a 1.2 run. Let the runs finish or stop them, and disable the pipelines' schedules so "
            "that no new run starts; then run polyris-up again.",
            f"To upgrade now, pass {_c(OVERRIDE_FLAG, CYAN)}: polyris-up then stops what 1.2 left running, and "
            "prints the runs to start again after the redeploy.")


# ── What 1.2 left running ────────────────────────────────────────────────────


@dataclass(frozen=True)
class StoppedRun:
    """A run that the stop ended, for the user to start again: a pipeline's execution, or a backfill's."""

    machine: str            # the state machine's name
    name: str               # the execution's
    started: datetime


@dataclass
class Stopped:
    """What ``stop_leftovers`` ended."""

    runs: List[StoppedRun] = field(default_factory=list)
    #: Executions of the engine's own state machines (wrappers, ``run_task``), which belong to those runs.
    engine: int = 0


def rows_engine_since(dynamodb: Any, target: Target) -> Optional[datetime]:
    """When the 1.3 update ran on this platform: the creation of ``pipeline-specs``, the table that update adds.

    No run of 1.3 can have started before it, so what started earlier is
    1.2's. The table is still there on a later ``polyris-up``, which finds
    1.2's leftovers by it again. None for a stack that names no such table, or
    whose table is gone.
    """
    if not target.specs_table:
        return None
    try:
        created = dynamodb.describe_table(TableName=target.specs_table)["Table"].get("CreationDateTime")
    except ClientError as e:
        if _code(e) != "ResourceNotFoundException":
            raise
        return None
    return created if isinstance(created, datetime) else None


def stop_leftovers(
    sfn: Any,
    *,
    target: Target,
    pipelines: Sequence[str],
    started_before: Optional[datetime],
    sleep: Callable[[float], None] = time.sleep,
) -> Stopped:
    """Stop what 1.2 left running on a platform that runs 1.3 (ADR #137, *Migration and upgrade*).

    A wrapper that waits on its token never ends, and the cleanup that deletes
    its state machine waits for it; a pipeline's run left running blocks the
    deletion of the pipeline's stack. Nothing adopts a 1.2 run, so:

    - **Which.** Every execution of the Standard state machines that 1.3
      deletes: none of them can be 1.3's. And every execution of
      ``bulk_backfill``, of the pipelines' state machines (``pipelines``,
      their ARNs) and of ``run_task`` that started before ``started_before``,
      the time the 1.3 update ran, so a pipeline redeployed since keeps its
      1.3 runs. With None that time is unknown, and those are left alone.
    - **Order.** What starts other executions first: ``bulk_backfill``, the
      pipelines' executions and the wrappers; then ``run_task``.
    - **Until.** It lists again until a listing shows nothing it has not
      stopped: a wrapper can start ``run_task`` between a listing and its
      stop, and ``ListExecutions`` shows a new execution only after a moment.

    It is idempotent: with nothing left, it stops nothing.
    """
    order: List[Tuple[str, Optional[datetime]]] = [(target.machine(name), None) for name in REMOVED_STANDARD_MACHINES]
    starters: Set[str] = set()
    if started_before is not None:
        starters = {target.machine(BULK_BACKFILL_MACHINE), *pipelines}
        order = [(target.machine(BULK_BACKFILL_MACHINE), started_before),
                 *((pipeline, started_before) for pipeline in pipelines),
                 *order,
                 (target.machine(RUN_TASK_MACHINE), started_before)]
    stopped = Stopped()
    seen: Set[str] = set()
    while True:
        before = len(seen)
        for machine, cutoff in order:
            for execution in _running(sfn, machine, cutoff):
                if execution["executionArn"] in seen:
                    continue                               # stopped already: a listing can lag behind a stop
                seen.add(execution["executionArn"])
                if not _stop(sfn, execution["executionArn"]):
                    continue
                if machine in starters:
                    stopped.runs.append(StoppedRun(machine=machine.rsplit(":", 1)[-1], name=execution["name"],
                                                   started=execution["startDate"]))
                else:
                    stopped.engine += 1
        if len(seen) == before:
            return stopped
        sleep(STOP_SETTLE_S)


def _running(sfn: Any, machine: str, before: Optional[datetime]) -> List[Dict[str, Any]]:
    """A state machine's running executions that started before ``before`` (all of them, with None).

    None of a state machine that does not exist: a pipeline destroyed since
    its registration, or one of 1.2's that CloudFormation has deleted.
    """
    executions: List[Dict[str, Any]] = []
    try:
        for page in sfn.get_paginator("list_executions").paginate(stateMachineArn=machine, statusFilter="RUNNING"):
            executions.extend(e for e in page.get("executions", []) if before is None or e["startDate"] < before)
    except ClientError as e:
        if _code(e) != "StateMachineDoesNotExist":
            raise
    return executions


def _stop(sfn: Any, execution: str) -> bool:
    """Stop an execution; False when it is gone already."""
    try:
        sfn.stop_execution(executionArn=execution, error=STOP_ERROR, cause=STOP_CAUSE)
    except ClientError as e:
        if _code(e) != "ExecutionDoesNotExist":
            raise
        return False
    return True


# ── The index that 1.3 adds ──────────────────────────────────────────────────


def wait_for_index(dynamodb: Any, target: Target, *, sleep: Callable[[float], None] = time.sleep) -> bool:
    """Wait until DynamoDB has built ``run-start-index`` on the tasks table, and say so (ADR #137 §5).

    The 1.3 update adds the index, and CloudFormation reports the table updated
    while DynamoDB still builds it: about 8 minutes on 100,000 rows (TR-0).
    Until it is active a read of it fails, and the console's run lists say that
    it is being built. True once it is active; False for a stack that names no
    tasks table, a table without the index, and an index still being built
    after an hour.
    """
    table, index = target.tokens_table, run_row.START_INDEX
    if not table:
        return False
    waited = 0.0
    while True:
        status = _index_status(dynamodb, table, index)
        if status == "ACTIVE":
            if waited:
                print(f"   {index} is active")
            return True
        if status is None:
            print(_c(f"   {table} has no {index}, which the console's run lists read. The stack's events name "
                     "what went wrong with the table.", YELLOW))
            return False
        if waited >= INDEX_WAIT_S:
            print(_c(f"   {index} is still being built. The console's run lists say so until it is active; "
                     "nothing else waits for it.", YELLOW))
            return False
        if not waited:
            print(f"   waiting for DynamoDB to build {index} on {table}, which the console's run lists read: "
                  "about 8 minutes on 100,000 rows")
        sleep(INDEX_POLL_S)
        waited += INDEX_POLL_S


def _index_status(dynamodb: Any, table: str, index: str) -> Optional[str]:
    """``ACTIVE``, ``CREATING``… of a table's global secondary index; None when it has no such index."""
    indexes = dynamodb.describe_table(TableName=table)["Table"].get("GlobalSecondaryIndexes") or []
    return next((str(found.get("IndexStatus", "")) for found in indexes if found.get("IndexName") == index), None)

