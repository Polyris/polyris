"""The move of a platform from 1.2 to 1.3, as ``polyris-up`` makes it (ADR #137, *Migration and upgrade*).

1.3 runs tasks as rows and deletes 1.2's engine, and nothing adopts a 1.2 run.
Before the update, ``check`` refuses a platform that is older than 1.2.0, whose
1.2 has not finished adopting older rows and facts for 48 hours, or that has a
run in flight.

The rules are pure and live in ``polyris/platform.py``. This module is their
AWS side: it reads through the boto3 clients its caller passes. ``polyris/up.py``
decides when each step runs and prints what it raises.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Mapping, Optional

from botocore.exceptions import ClientError

from ._ansi import CYAN
from ._ansi import c as _c
from .constants import ACCESS_DENIED_CODES
from .coordination import run_row, sweep
from .platform import (
    ADOPTION_SETTLE,
    BULK_BACKFILL_MACHINE,
    OLDEST_UPGRADABLE,
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


class Refused(Exception):
    """A check refused the upgrade: the reason, then what to do about it."""

    def __init__(self, reason: str, *todo: str):
        super().__init__(reason)
        self.todo = todo


@dataclass(frozen=True)
class Target:
    """The deployed platform a step acts on: its stack, where its state machines are, and its tasks table."""

    stack_name: str
    #: The ARN of any of its state machines, up to the end of ``{Namespace}-{Stage}-polyris-``.
    machine_prefix: str
    #: The tasks table (``pipeline-tokens``); empty when the stack names none.
    tokens_table: str

    @classmethod
    def of(cls, stack: Mapping[str, Any], *, partition: str, region: str, account: str) -> "Target":
        """The target a describe_stacks entry describes, in this account and region."""
        params = parameters_of(stack)
        prefix = platform_prefix(params.get(PARAM_NAMESPACE, ""), params.get(PARAM_STAGE, ""))
        return cls(
            stack_name=str(stack.get("StackName", "")),
            machine_prefix=state_machine_arn(partition=partition, region=region, account=account, name=prefix),
            tokens_table=outputs_of(stack).get(OUTPUT_TOKENS_TABLE, ""),
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
        if e.response.get("Error", {}).get("Code", "") not in ACCESS_DENIED_CODES:
            raise
        raise Refused(
            f"polyris-up may not call {action} on {resource}, which its checks before the upgrade to 1.3 need.",
            "Grant it to the role that runs polyris-up (docs/deployment/IAM_PERMISSIONS.md), then run it again.",
        ) from e


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
            if e.response.get("Error", {}).get("Code", "") != "StateMachineDoesNotExist":
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
            f"To upgrade now, pass {_c(OVERRIDE_FLAG, CYAN)}: no run of 1.2 goes on after the upgrade, so start "
            "them again after the redeploy.")
