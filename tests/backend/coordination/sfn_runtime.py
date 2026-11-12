"""A small Step Functions interpreter for the coordination harness (ADR #130, Testing).

It runs the JSONata-mode state machines from sam/sfn_templates and the
generated pipeline ASL, state by state, against moto DynamoDB and in-process
Lambda handlers. Scenarios therefore exercise the real graphs, expressions,
writes and token flows, and they keep working unchanged while the templates
change from phase to phase.

Modelled: Pass, Task, Choice, Parallel, Map, Wait (instant), Succeed, Fail;
Arguments / Output / Assign / Items / ItemSelector / Condition; variables
copied into Parallel branches and Map iterations; Retry (immediate) and
Catch; task tokens (`.waitForTaskToken`), `.sync` children, express
`startSyncExecution`, fire-and-forget starts, stops and timeouts on request.
The runtime also serves the Step Functions API calls the Lambdas make
(`send_task_success`, `describe_execution`, ...): one object is both the
interpreter and the boto3 client the code under test talks to.

Not modelled: real time (timeouts are explicit test actions), AWS's own
JSONata dialect (the live smoke and TestState runs cover it), quotas.
"""

from __future__ import annotations

import decimal
import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import jsonata
from botocore.exceptions import ClientError

ACCOUNT = "123456789012"
REGION = "us-east-1"
TOKEN_SUFFIX = ".waitForTaskToken"


class StatesError(Exception):
    """A Step Functions error: what Retry and Catch match on."""

    def __init__(self, error: str, cause: str = ""):
        super().__init__(f"{error}: {cause}")
        self.error = error
        self.cause = cause


def client_error(code: str, operation: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


def _lambda_json(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def matches(error: str, names: List[str]) -> bool:
    """ErrorEquals semantics: States.ALL catches all, States.TaskFailed all but timeouts."""
    if "States.ALL" in names or error in names:
        return True
    return "States.TaskFailed" in names and error != "States.Timeout"


class Expressions:
    """Evaluates `{% ... %}` values the way a JSONata-mode state machine does."""

    def __init__(self, now: Callable[[], str]):
        self.now = now
        self._uuids = itertools.count(1)

    def evaluate(self, value: Any, *, states: Dict[str, Any], variables: Dict[str, Any]) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{%") and text.endswith("%}"):
                return self._run(text[2:-2].strip(), states, variables)
            return value
        if isinstance(value, dict):
            return {k: self.evaluate(v, states=states, variables=variables) for k, v in value.items()}
        if isinstance(value, list):
            return [self.evaluate(v, states=states, variables=variables) for v in value]
        return value

    def _run(self, expression: str, states: Dict[str, Any], variables: Dict[str, Any]) -> Any:
        compiled = jsonata.Jsonata(expression)
        compiled.assign("states", states)
        for name, val in variables.items():
            compiled.assign(name, val)
        compiled.register_lambda("now", self.now)
        compiled.register_lambda("parse", json.loads)
        compiled.register_lambda("hash", lambda text, algorithm: hashlib.new(
            str(algorithm).replace("-", "").lower(), str(text).encode("utf-8")).hexdigest())
        compiled.register_lambda("uuid", lambda: f"00000000-0000-4000-8000-{next(self._uuids):012d}")
        try:
            return compiled.evaluate(None)
        except Exception as exc:   # noqa: BLE001 — AWS reports every evaluation failure as this error
            raise StatesError("States.QueryEvaluationError", f"{expression}: {exc}") from exc


@dataclass
class Machine:
    arn: str
    name: str
    definition: Dict[str, Any]
    type: str = "STANDARD"


@dataclass
class Execution:
    arn: str
    name: str
    machine: Machine
    input: Any
    started: str
    status: str = "RUNNING"
    output: Any = None
    error: Optional[str] = None
    cause: Optional[str] = None
    waiter: Optional["Frame"] = None   # a `.sync` parent waiting for this execution
    sync_shape: str = ""


@dataclass(eq=False)
class Frame:
    """One thread of control: an execution's top level, a Parallel branch or a Map iteration."""

    execution: Execution
    states: Dict[str, Any]
    state: str
    input: Any
    variables: Dict[str, Any]
    parent: Optional["Frame"] = None
    slot: int = 0
    waiting: Optional[str] = None                 # what the frame waits for, if it waits
    resume: Optional[Dict[str, Any]] = None       # how it resumes: {"result": ...} or {"error": ...}
    children: List["Frame"] = field(default_factory=list)
    results: List[Any] = field(default_factory=list)
    open_children: int = 0
    attempts: Dict[int, int] = field(default_factory=dict)
    done: bool = False


@dataclass
class ExternalJob:
    """A `.sync` call to something outside the platform — the user's task. Tests finish it."""

    id: str
    resource: str
    arguments: Dict[str, Any]
    frame: Frame


class Runtime:
    def __init__(self, ddb_client: Any, *, now: Callable[[], str],
                 lambdas: Optional[Dict[str, Callable[[Any, Any], Any]]] = None):
        self.ddb = ddb_client
        self.now = now
        self.expressions = Expressions(now)
        self.lambdas: Dict[str, Callable[[Any, Any], Any]] = dict(lambdas or {})
        self.machines: Dict[str, Machine] = {}
        self.executions: Dict[str, Execution] = {}
        self.runnable: List[Frame] = []
        #: Machines whose new executions wait to take their first step: {arn: their frames}.
        self.held: Dict[str, List[Frame]] = {}
        self.tokens: Dict[str, Frame] = {}
        self.jobs: Dict[str, ExternalJob] = {}
        self.side_effects: List[Dict[str, Any]] = []
        #: Every accepted token callback: {"execution", "state", "outcome"}.
        self.callbacks: List[Dict[str, Any]] = []
        self._ids = itertools.count(1)

    # ── registry ────────────────────────────────────────────────────────

    def add_machine(self, name: str, definition: Dict[str, Any], *, type: str = "STANDARD") -> Machine:
        machine = Machine(f"arn:aws:states:{REGION}:{ACCOUNT}:stateMachine:{name}", name, definition, type)
        self.machines[machine.arn] = machine
        return machine

    # ── the Step Functions API the code under test calls ───────────────

    def start_execution(self, *, stateMachineArn: str, name: Optional[str] = None,
                        input: str = "{}") -> Dict[str, Any]:
        execution = self._start(stateMachineArn, name, json.loads(input))
        return {"executionArn": execution.arn, "startDate": execution.started}

    def start_sync_execution(self, *, stateMachineArn: str, name: Optional[str] = None,
                             input: str = "{}") -> Dict[str, Any]:
        execution = self._start(stateMachineArn, name, json.loads(input))
        self._run_only(execution)
        return self._describe(execution)

    def send_task_success(self, *, taskToken: str, output: str) -> Dict[str, Any]:
        frame = self._take_token(taskToken, "SendTaskSuccess")
        self._callback(frame, {"result": json.loads(output)})
        return {}

    def send_task_failure(self, *, taskToken: str, error: str = "", cause: str = "") -> Dict[str, Any]:
        frame = self._take_token(taskToken, "SendTaskFailure")
        self._callback(frame, {"error": error or "States.TaskFailed", "cause": cause})
        return {}

    def describe_execution(self, *, executionArn: str) -> Dict[str, Any]:
        if executionArn not in self.executions:
            raise client_error("ExecutionDoesNotExist", "DescribeExecution")
        return self._describe(self.executions[executionArn])

    def stop_execution(self, *, executionArn: str, error: str = "", cause: str = "") -> Dict[str, Any]:
        execution = self.executions.get(executionArn)
        if execution is None:
            raise client_error("ExecutionDoesNotExist", "StopExecution")
        if execution.status == "RUNNING":
            self._finish(execution, "ABORTED", error=error or "States.Aborted", cause=cause)
        return {"stopDate": self.now()}

    # ── test controls ──────────────────────────────────────────────────

    def run(self, max_steps: int = 100_000) -> None:
        """Run every runnable frame until all of them wait or finish."""
        for _ in range(max_steps):
            if not self.runnable:
                return
            frame = self.runnable.pop(0)
            if not frame.done and frame.execution.status == "RUNNING":
                self._step(frame)
        raise RuntimeError("state machines did not settle; is there a loop?")

    def hold(self, machine_arn: str) -> None:
        """New executions of `machine_arn` start but take no step until `release` (to order a race)."""
        self.held.setdefault(machine_arn, [])

    def release(self, machine_arn: str) -> None:
        self.runnable.extend(self.held.pop(machine_arn, []))

    def time_out(self, token: str) -> None:
        """The task waiting on `token` reaches its TimeoutSeconds."""
        frame = self._take_token(token, "Timeout")
        self._wake(frame, {"error": "States.Timeout", "cause": "the task timed out"})

    def finish_job(self, job_id: str, *, output: Any = None, error: str = "", cause: str = "") -> None:
        """Complete an external `.sync` job (the user's task)."""
        job = self.jobs.pop(job_id)
        self._wake(job.frame, {"error": error, "cause": cause} if error else {"result": output or {}})

    # ── internals: executions ───────────────────────────────────────────

    def _start(self, machine_arn: str, name: Optional[str], payload: Any) -> Execution:
        machine = self.machines.get(machine_arn)
        if machine is None:
            raise client_error("StateMachineDoesNotExist", "StartExecution", machine_arn)
        name = name or f"exec-{next(self._ids)}"
        arn = f"arn:aws:states:{REGION}:{ACCOUNT}:execution:{machine.name}:{name}"
        existing = self.executions.get(arn)
        if existing is not None and machine.type == "STANDARD":
            # StartExecution is idempotent for a running execution with the same input.
            if existing.status == "RUNNING" and existing.input == payload:
                return existing
            raise client_error("ExecutionAlreadyExists", "StartExecution", arn)
        execution = Execution(arn, name, machine, payload, self.now())
        self.executions[arn] = execution
        frame = Frame(execution, machine.definition["States"], machine.definition["StartAt"], payload, {})
        if machine_arn in self.held:
            self.held[machine_arn].append(frame)
        else:
            self.runnable.append(frame)
        return execution

    def _run_only(self, execution: Execution, max_steps: int = 10_000) -> None:
        """Run one execution to its end now; other executions keep their place in the queue."""
        for _ in range(max_steps):
            frame = next((f for f in self.runnable if f.execution is execution), None)
            if frame is None:
                break
            self.runnable.remove(frame)
            if not frame.done and execution.status == "RUNNING":
                self._step(frame)
        if execution.status == "RUNNING":
            raise RuntimeError(f"express execution {execution.name} did not finish; express machines cannot wait")

    def _finish(self, execution: Execution, status: str, *, output: Any = None,
                error: Optional[str] = None, cause: Optional[str] = None) -> None:
        execution.status, execution.output, execution.error, execution.cause = status, output, error, cause
        for token in [t for t, f in self.tokens.items() if f.execution is execution]:
            del self.tokens[token]
        for job_id in [j for j, job in self.jobs.items() if job.frame.execution is execution]:
            del self.jobs[job_id]
        self.runnable = [f for f in self.runnable if f.execution is not execution]
        waiter = execution.waiter
        if waiter is None:
            return
        if status == "SUCCEEDED":
            serialized = output if execution.sync_shape == "sync2" else json.dumps(output)
            self._wake(waiter, {"result": {"ExecutionArn": execution.arn, "Output": serialized, "Status": status}})
        else:
            self._wake(waiter, {"error": "States.TaskFailed",
                                "cause": json.dumps({"Status": status, "Error": error, "Cause": cause})})

    def _describe(self, execution: Execution) -> Dict[str, Any]:
        described = {"executionArn": execution.arn, "name": execution.name, "status": execution.status,
                     "stateMachineArn": execution.machine.arn, "input": json.dumps(execution.input)}
        if execution.status == "SUCCEEDED":
            described["output"] = json.dumps(execution.output)
        if execution.error:
            described.update(error=execution.error, cause=execution.cause or "")
        return described

    def _take_token(self, token: str, operation: str) -> Frame:
        frame = self.tokens.pop(token, None)
        if frame is None:
            raise client_error("TaskTimedOut", operation, "the task token is no longer valid")
        return frame

    def _callback(self, frame: Frame, outcome: Dict[str, Any]) -> None:
        self.callbacks.append({"execution": frame.execution.arn, "state": frame.state, "outcome": outcome})
        self._wake(frame, outcome)

    def _wake(self, frame: Frame, outcome: Dict[str, Any]) -> None:
        frame.waiting = None
        frame.resume = outcome
        self.runnable.append(frame)

    def _cancel(self, frame: Frame) -> None:
        """A sibling failed: stop this frame and everything under it, voiding its tokens and jobs."""
        frame.done = True
        for child in frame.children:
            self._cancel(child)
        self.tokens = {t: f for t, f in self.tokens.items() if f is not frame}
        self.jobs = {j: job for j, job in self.jobs.items() if job.frame is not frame}

    # ── internals: states ───────────────────────────────────────────────

    def _context(self, frame: Frame, **extra: Any) -> Dict[str, Any]:
        execution = frame.execution
        context: Dict[str, Any] = {
            "Execution": {"Id": execution.arn, "Name": execution.name, "StartTime": execution.started,
                          "Input": execution.input},
            "StateMachine": {"Id": execution.machine.arn, "Name": execution.machine.name},
            "State": {"Name": frame.state, "EnteredTime": self.now()},
        }
        context.update(extra)
        return context

    def _eval(self, frame: Frame, value: Any, context: Optional[Dict[str, Any]] = None, **states: Any) -> Any:
        bound = {"input": frame.input, "context": context or self._context(frame)}
        bound.update(states)
        return self.expressions.evaluate(value, states=bound, variables=frame.variables)

    def _step(self, frame: Frame) -> None:
        state = frame.states[frame.state]
        kind = state["Type"]
        try:
            if frame.resume is not None:
                outcome, frame.resume = frame.resume, None
                if outcome.get("error"):
                    raise StatesError(outcome["error"], outcome.get("cause", ""))
                self._complete(frame, state, outcome.get("result"))
            elif kind in ("Pass", "Wait"):
                self._complete(frame, state, frame.input)
            elif kind == "Choice":
                self._choice(frame, state)
            elif kind == "Succeed":
                self._end(frame, self._eval(frame, state["Output"]) if "Output" in state else frame.input)
            elif kind == "Fail":
                raise StatesError(str(self._eval(frame, state.get("Error", "States.Fail"))),
                                  str(self._eval(frame, state.get("Cause", ""))))
            elif kind == "Parallel":
                payload = self._eval(frame, state["Arguments"]) if "Arguments" in state else frame.input
                self._fork(frame, [(b["States"], b["StartAt"], payload) for b in state["Branches"]])
            elif kind == "Map":
                self._map(frame, state)
            elif kind == "Task":
                self._task(frame, state)
            else:
                raise NotImplementedError(f"state type {kind}")
        except StatesError as err:
            self._fail(frame, state, err)

    def _complete(self, frame: Frame, state: Dict[str, Any], result: Any) -> None:
        """Apply Output and Assign (both see the variables as they were), then move on."""
        produces = state["Type"] in ("Task", "Parallel", "Map")
        states: Dict[str, Any] = {"result": result} if produces else {}
        if "Output" in state:
            output = self._eval(frame, state["Output"], **states)
        else:
            output = result if produces else frame.input
        if "Assign" in state:
            frame.variables = {**frame.variables, **self._eval(frame, state["Assign"], **states)}
        if state.get("End"):
            self._end(frame, output)
            return
        frame.state, frame.input, frame.attempts = state["Next"], output, {}
        self.runnable.append(frame)

    def _choice(self, frame: Frame, state: Dict[str, Any]) -> None:
        rule: Dict[str, Any] = next((r for r in state.get("Choices", [])
                                     if self._eval(frame, r["Condition"]) is True), {})
        if not rule:
            if "Default" not in state:
                raise StatesError("States.NoChoiceMatched", f"no choice matched in {frame.state}")
            rule = {"Next": state["Default"]}
        output_spec = rule.get("Output", state.get("Output"))
        output = frame.input if output_spec is None else self._eval(frame, output_spec)
        assign = rule.get("Assign", state.get("Assign"))
        if assign:
            frame.variables = {**frame.variables, **self._eval(frame, assign)}
        frame.state, frame.input, frame.attempts = rule["Next"], output, {}
        self.runnable.append(frame)

    def _end(self, frame: Frame, output: Any) -> None:
        frame.done = True
        if frame.parent is None:
            self._finish(frame.execution, "SUCCEEDED", output=output)
            return
        parent = frame.parent
        parent.results[frame.slot] = output
        parent.open_children -= 1
        if parent.open_children == 0:
            self._wake(parent, {"result": list(parent.results)})

    def _fail(self, frame: Frame, state: Dict[str, Any], err: StatesError) -> None:
        for index, retrier in enumerate(state.get("Retry", [])):
            if matches(err.error, retrier["ErrorEquals"]):
                used = frame.attempts.get(index, 0)
                if used < retrier.get("MaxAttempts", 3):
                    frame.attempts[index] = used + 1
                    self.runnable.append(frame)
                    return
                break
        for catcher in state.get("Catch", []):
            if matches(err.error, catcher["ErrorEquals"]):
                error_output = {"Error": err.error, "Cause": err.cause}
                output = self._eval(frame, catcher["Output"], errorOutput=error_output) \
                    if "Output" in catcher else error_output
                if "Assign" in catcher:
                    frame.variables = {**frame.variables,
                                       **self._eval(frame, catcher["Assign"], errorOutput=error_output)}
                frame.state, frame.input, frame.attempts = catcher["Next"], output, {}
                self.runnable.append(frame)
                return
        frame.done = True
        if frame.parent is None:
            self._finish(frame.execution, "FAILED", error=err.error, cause=err.cause)
            return
        parent = frame.parent
        for sibling in parent.children:
            if sibling is not frame:
                self._cancel(sibling)
        self._wake(parent, {"error": err.error, "cause": err.cause})

    def _fork(self, frame: Frame, branches: List[Any]) -> None:
        frame.results = [None] * len(branches)
        frame.children = []
        frame.open_children = len(branches)
        if not branches:
            self._wake(frame, {"result": []})
            return
        frame.waiting = "children"
        for slot, (states, start, payload) in enumerate(branches):
            child = Frame(frame.execution, states, start, payload, dict(frame.variables), parent=frame, slot=slot)
            frame.children.append(child)
            self.runnable.append(child)

    def _map(self, frame: Frame, state: Dict[str, Any]) -> None:
        items = self._eval(frame, state["Items"]) if "Items" in state else frame.input
        if not isinstance(items, list):
            raise StatesError("States.QueryEvaluationError", f"Map items of {frame.state} are not a list")
        processor = state.get("ItemProcessor") or state["Iterator"]
        branches = []
        for index, item in enumerate(items):
            if "ItemSelector" in state:
                context = self._context(frame, Map={"Item": {"Index": index, "Value": item}})
                item = self._eval(frame, state["ItemSelector"], context=context)
            branches.append((processor["States"], processor["StartAt"], item))
        self._fork(frame, branches)

    # ── internals: integrations ─────────────────────────────────────────

    def _task(self, frame: Frame, state: Dict[str, Any]) -> None:
        resource = state["Resource"]
        if not resource.endswith(TOKEN_SUFFIX):
            arguments = self._eval(frame, state.get("Arguments", {}))
            self._dispatch(frame, state, resource, arguments, token=None)
            return
        token = f"token-{next(self._ids)}:{frame.execution.name}:{frame.state}"
        arguments = self._eval(frame, state.get("Arguments", {}), context=self._context(frame, Task={"Token": token}))
        # Registered before the call: a Lambda may send the token back before it returns.
        self.tokens[token] = frame
        frame.waiting = token
        try:
            self._dispatch(frame, state, resource[: -len(TOKEN_SUFFIX)], arguments, token=token)
        except StatesError:
            self.tokens.pop(token, None)
            frame.waiting = None
            raise

    def _dispatch(self, frame: Frame, state: Dict[str, Any], resource: str,
                  arguments: Dict[str, Any], token: Optional[str]) -> None:
        if resource.startswith(("arn:aws:states:::dynamodb:", "arn:aws:states:::aws-sdk:dynamodb:")):
            result: Any = self._dynamodb(resource, arguments)
        elif resource == "arn:aws:states:::lambda:invoke":
            result = {"Payload": self._invoke(arguments["FunctionName"], arguments.get("Payload")), "StatusCode": 200}
        elif resource.startswith("arn:aws:lambda:"):
            result = self._invoke(resource, arguments)
        elif resource.startswith("arn:aws:states:::states:startExecution"):
            result = self._start_child(frame, resource, arguments, waits=token is None)
        elif resource.startswith("arn:aws:states:::aws-sdk:sfn:"):
            result = self._sfn_sdk(resource.rsplit(":", 1)[1], arguments)
        elif resource.endswith(".sync"):
            self._external(frame, resource, arguments)
            return
        else:
            self.side_effects.append({"resource": resource, "arguments": arguments})
            result = {}
        if token is None and frame.waiting is None:
            self._complete(frame, state, result)

    def _dynamodb(self, resource: str, arguments: Dict[str, Any]) -> Any:
        operation = resource.rsplit(":", 1)[1]
        method = "".join("_" + c.lower() if c.isupper() else c for c in operation)
        prefix = "DynamoDB" if ":::dynamodb:" in resource else "DynamoDb"
        try:
            response = getattr(self.ddb, method)(**arguments)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            raise StatesError(f"{prefix}.{code}", str(exc)) from exc
        response.pop("ResponseMetadata", None)
        return response

    def _invoke(self, function: str, payload: Any) -> Any:
        name = function.rsplit(":", 1)[-1]
        handler = self.lambdas.get(name)
        if handler is None:
            raise StatesError("Lambda.ResourceNotFoundException", f"no in-process handler for {name}")
        try:
            result = handler(json.loads(json.dumps(payload)), None)
        except Exception as exc:   # noqa: BLE001 — a Lambda error reaches the state under its error type
            raise StatesError(type(exc).__name__, json.dumps({"errorMessage": str(exc)})) from exc
        try:
            # The Lambda runtime returns JSON; like its marshaller, numbers from DynamoDB become floats.
            return json.loads(json.dumps(result, default=_lambda_json))
        except TypeError as exc:
            raise StatesError("Runtime.MarshalError", str(exc)) from exc

    def _start_child(self, frame: Frame, resource: str, arguments: Dict[str, Any], *, waits: bool) -> Any:
        machine_arn = arguments["StateMachineArn"]
        payload = arguments.get("Input", {})
        if isinstance(payload, str):
            payload = json.loads(payload)
        if machine_arn not in self.machines:
            # The user's own state machine: a `.sync` call waits for the test to finish it.
            if waits and resource.endswith((".sync", ".sync:2")):
                self._external(frame, resource, arguments)
                return None
            self.side_effects.append({"resource": resource, "arguments": arguments})
            return {"ExecutionArn": f"{machine_arn}:external", "StartDate": self.now()}
        try:
            child = self._start(machine_arn, arguments.get("Name"), payload)
        except ClientError as exc:
            raise StatesError(f"StepFunctions.{exc.response['Error']['Code']}Exception", str(exc)) from exc
        if waits and (resource.endswith(".sync:2") or resource.endswith(".sync")):
            child.waiter, child.sync_shape = frame, ("sync2" if resource.endswith(":2") else "sync")
            frame.waiting = f"execution:{child.arn}"
        return {"ExecutionArn": child.arn, "StartDate": child.started}

    def _sfn_sdk(self, operation: str, arguments: Dict[str, Any]) -> Any:
        call = {
            "sendTaskSuccess": self.send_task_success, "sendTaskFailure": self.send_task_failure,
            "startSyncExecution": self.start_sync_execution, "startExecution": self.start_execution,
            "stopExecution": self.stop_execution, "describeExecution": self.describe_execution,
        }[operation]
        normalized = {}
        for key, value in arguments.items():
            if key in ("Input", "Output") and not isinstance(value, str):
                value = json.dumps(value)
            normalized[key[0].lower() + key[1:]] = value
        try:
            response = call(**normalized)
        except ClientError as exc:
            raise StatesError(f"Sfn.{exc.response['Error']['Code']}Exception", str(exc)) from exc
        return {k[0].upper() + k[1:]: v for k, v in response.items()}

    def _external(self, frame: Frame, resource: str, arguments: Dict[str, Any]) -> None:
        job = ExternalJob(f"job-{next(self._ids)}", resource, arguments, frame)
        self.jobs[job.id] = job
        frame.waiting = f"job:{job.id}"
