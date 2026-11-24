"""Stop the running executions of the state machines that a stack deletion removes.

CloudFormation deletes a state machine only once its running executions have ended, and a run of 1.3
keeps its execution open until every task of it is final; a paused run never ends by itself (ADR #137
§1, §5). So a command that deletes a stack with state machines stops their executions until the stack
is gone: ``polyris-destroy`` a pipeline's runs, ``polyris-down`` the platform's own executions, and
``polyris-up`` what 1.2 left running (``upgrade.stop_leftovers``). The engine stops the tasks of a run
whose execution was stopped at its next check of the run (ADR #137 §4).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from botocore.exceptions import ClientError

#: The resource type of a state machine in a stack's resources.
STATE_MACHINE = "AWS::StepFunctions::StateMachine"
#: How many executions one listing asks for, the most that ListExecutions gives.
PAGE = 1000


def error_code(error: ClientError) -> str:
    """The code of an AWS error (``AccessDeniedException``, ``ExecutionDoesNotExist``…)."""
    return str(error.response.get("Error", {}).get("Code", ""))


def running(sfn: Any, machine: str, *, started_before: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """The running executions of ``machine`` that started before ``started_before`` (all of them, with None).

    None of a state machine that does not exist: one that CloudFormation has deleted, or a pipeline
    destroyed since its registration.
    """
    executions: List[Dict[str, Any]] = []
    try:
        pages = sfn.get_paginator("list_executions").paginate(
            stateMachineArn=machine, statusFilter="RUNNING", PaginationConfig={"PageSize": PAGE})
        for page in pages:
            executions.extend(e for e in page.get("executions", [])
                              if started_before is None or e["startDate"] < started_before)
    except ClientError as e:
        if error_code(e) != "StateMachineDoesNotExist":
            raise
    return executions


def stop(sfn: Any, execution: str, *, error: str, cause: str) -> bool:
    """Stop an execution; False when it is gone already."""
    try:
        sfn.stop_execution(executionArn=execution, error=error, cause=cause)
    except ClientError as e:
        if error_code(e) != "ExecutionDoesNotExist":
            raise
        return False
    return True


def stop_running(
    sfn: Any,
    machines: Sequence[Tuple[str, Optional[datetime]]],
    *,
    error: str,
    cause: str,
    seen: Set[str],
) -> List[Tuple[str, Dict[str, Any]]]:
    """Stop the running executions of each state machine, in order, that started before its time (all, with None).

    ``seen`` holds the executions stopped before, which it skips: a listing can lag behind a stop. It
    adds each execution it meets. Returns each execution it stopped, with its state machine.
    """
    stopped: List[Tuple[str, Dict[str, Any]]] = []
    for machine, started_before in machines:
        for execution in running(sfn, machine, started_before=started_before):
            if execution["executionArn"] in seen:
                continue
            seen.add(execution["executionArn"])
            if stop(sfn, execution["executionArn"], error=error, cause=cause):
                stopped.append((machine, execution))
    return stopped


def stack_machines(cfn: Any, stack: str) -> List[str]:
    """The ARNs of the state machines in ``stack``; none when the stack does not exist."""
    machines: List[str] = []
    try:
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
            machines.extend(r["PhysicalResourceId"] for r in page.get("StackResourceSummaries", [])
                            if r["ResourceType"] == STATE_MACHINE and r.get("PhysicalResourceId"))
    except ClientError as e:
        if "does not exist" not in str(e):
            raise
    return machines
