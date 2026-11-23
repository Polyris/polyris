"""Step Functions for the tests of ``polyris-up``'s upgrade steps, behind a real boto3 client.

The client is real, so botocore validates every request against the service's
model before it is answered: a parameter AWS would refuse fails the test. The
answers come from a small world of state machines and executions that a test
builds, and each answer is validated against the model too.

moto does not fit here. It dates an execution in local time, so "started
before" would depend on the developer's time zone; it lists nothing for an
Express state machine, where AWS refuses the call; and it shows a stop at
once, where AWS's ``ListExecutions`` is eventually consistent.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import boto3
from botocore.awsrequest import AWSResponse
from botocore.validate import validate_parameters

ACCOUNT = "123456789012"
REGION = "eu-central-1"
PREFIX = f"arn:aws:states:{REGION}:{ACCOUNT}"
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def refuse(client: Any, operation: str, code: str = "AccessDeniedException") -> None:
    """Make a real client answer ``operation`` (``GetItem``, ``Scan``…) with an error, before the call is sent.

    Registered on the wildcard event: botocore calls a wildcard's handlers before those of the
    operation's own event, so only there does it come before another answer of the same client.
    """
    def answer(model, **_):
        if model.name != operation:
            return None
        return _error(code, f"not authorized to perform {operation}")

    client.meta.events.register_first("before-call.*.*", answer)


@dataclass
class Execution:
    arn: str
    machine: str
    name: str
    started: datetime
    status: str = "RUNNING"
    #: How many more listings do not show it yet: ListExecutions is eventually consistent.
    hidden: int = 0
    #: How many more listings still show it as running after its stop.
    lingers: int = 0


class FakeStates:
    """State machines and their executions; ``client`` is the boto3 client that reaches them."""

    def __init__(self) -> None:
        self.client = boto3.client("stepfunctions", region_name=REGION, aws_access_key_id="testing",
                                   aws_secret_access_key="testing")
        self.machines: Dict[str, str] = {}                 # ARN → STANDARD | EXPRESS
        self.deleting: Set[str] = set()
        self.executions: List[Execution] = []
        self.stopped: List[str] = []                       # execution names, in the order they were stopped
        self.stops: List[Dict[str, Any]] = []              # the StopExecution requests
        self.calls: List[Tuple[str, Dict[str, Any]]] = []
        self.denied: Set[str] = set()                      # operations answered with AccessDeniedException
        self.page_size = 100
        #: Called with an execution's name just before its stop takes effect: what a race does meanwhile.
        self.on_stop: Optional[Callable[[str], None]] = None
        #: Executions (by name) that a listing still shows but that are gone when their stop arrives.
        self.gone_on_stop: Set[str] = set()
        self._params: Dict[str, Any] = {}
        self.client.meta.events.register_first("before-parameter-build.*.*", self._remember)
        self.client.meta.events.register("before-call.*.*", self._answer)

    # ── the world a test builds ──────────────────────────────────────────────

    def machine(self, name: str, *, express: bool = False, deleting: bool = False) -> str:
        arn = f"{PREFIX}:stateMachine:{name}"
        self.machines[arn] = "EXPRESS" if express else "STANDARD"
        if deleting:
            self.deleting.add(arn)
        return arn

    def run(self, machine: str, name: str, *, started: datetime, hidden: int = 0, lingers: int = 0) -> Execution:
        """A running execution ``name`` of the state machine named ``machine``."""
        arn = f"{PREFIX}:stateMachine:{machine}"
        assert arn in self.machines, f"no state machine {machine}"
        execution = Execution(arn=f"{PREFIX}:execution:{machine}:{name}", machine=arn, name=name, started=started,
                              hidden=hidden, lingers=lingers)
        self.executions.append(execution)
        return execution

    def running(self) -> List[str]:
        return sorted(e.name for e in self.executions if e.status == "RUNNING")

    def operations(self) -> List[str]:
        return [name for name, _ in self.calls]

    # ── the boto3 boundary ───────────────────────────────────────────────────

    def _remember(self, params, **_) -> None:
        self._params = dict(params)

    def _answer(self, model, **_):
        params, self._params = self._params, {}
        self.calls.append((model.name, params))
        if model.name in self.denied:
            return _error("AccessDeniedException", f"not authorized to perform states:{model.name}")
        answer = {"ListExecutions": self._list_executions, "StopExecution": self._stop_execution,
                  "ListStateMachines": self._list_state_machines,
                  "DescribeStateMachine": self._describe_state_machine}.get(model.name)
        assert answer is not None, f"FakeStates has no answer for {model.name}"
        result = answer(**params)
        if isinstance(result, tuple):
            return result
        validate_parameters(result, model.output_shape)
        return AWSResponse(None, 200, {}, None), result

    def _page(self, items: List[Dict[str, Any]], next_token: Optional[str], limit: int) -> Tuple[List[Dict[str, Any]], Dict]:
        start = int(next_token or 0)
        size = min(limit or self.page_size, self.page_size)
        more = {"nextToken": str(start + size)} if start + size < len(items) else {}
        return items[start:start + size], more

    def _list_executions(self, stateMachineArn, statusFilter=None, maxResults=0, nextToken=None):
        if stateMachineArn not in self.machines:
            return _error("StateMachineDoesNotExist", f"State Machine Does Not Exist: '{stateMachineArn}'")
        if self.machines[stateMachineArn] == "EXPRESS":
            return _error("StateMachineTypeNotSupported", "This operation is not supported by this type of state machine")
        listed = []
        for execution in sorted(self.executions, key=lambda e: e.started, reverse=True):
            if execution.machine != stateMachineArn:
                continue
            if nextToken is None and execution.hidden:
                execution.hidden -= 1
                continue
            status = execution.status
            if status == "ABORTED" and execution.lingers and nextToken is None:
                execution.lingers -= 1
                status = "RUNNING"
            if statusFilter in (None, status):
                listed.append({"executionArn": execution.arn, "stateMachineArn": stateMachineArn,
                               "name": execution.name, "status": status, "startDate": execution.started})
        page, more = self._page(listed, nextToken, maxResults)
        return {"executions": page, **more}

    def _stop_execution(self, executionArn, error=None, cause=None):
        execution = next((e for e in self.executions if e.arn == executionArn), None)
        if execution is None or execution.name in self.gone_on_stop:
            return _error("ExecutionDoesNotExist", f"Execution Does Not Exist: '{executionArn}'")
        if self.on_stop:
            self.on_stop(execution.name)
        execution.status = "ABORTED"
        self.stopped.append(execution.name)
        self.stops.append({"executionArn": executionArn, "error": error, "cause": cause})
        return {"stopDate": datetime(2026, 11, 23, tzinfo=timezone.utc)}

    def _list_state_machines(self, maxResults=0, nextToken=None):
        machines = [{"stateMachineArn": arn, "name": arn.rsplit(":", 1)[-1], "type": kind, "creationDate": CREATED}
                    for arn, kind in sorted(self.machines.items())]
        page, more = self._page(machines, nextToken, maxResults)
        return {"stateMachines": page, **more}

    def _describe_state_machine(self, stateMachineArn, includedData=None):
        if stateMachineArn not in self.machines:
            return _error("StateMachineDoesNotExist", f"State Machine Does Not Exist: '{stateMachineArn}'")
        return {"stateMachineArn": stateMachineArn, "name": stateMachineArn.rsplit(":", 1)[-1],
                "status": "DELETING" if stateMachineArn in self.deleting else "ACTIVE", "definition": "{}",
                "roleArn": f"arn:aws:iam::{ACCOUNT}:role/polyris", "type": self.machines[stateMachineArn],
                "creationDate": CREATED}


def _error(code: str, message: str):
    return AWSResponse(None, 400, {}, None), {"Error": {"Code": code, "Message": message},
                                              "ResponseMetadata": {"HTTPStatusCode": 400}}
