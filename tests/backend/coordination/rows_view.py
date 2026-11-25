"""What the stack holds and did, in the terms of ADR #137's invariants (row_invariants.RowsView).

The tables give the runs and their rows; the interpreter gives the run_task
executions. What no table keeps, the `Observer` records as it happens: each
start of run_task with its row as it stood then, each answer of a run's token
with the rows that were not final then, each pass of the dispatcher with what
it did. The boto3 boundary records each write of a task row, before and after
(`Boundary.watch`). None of it touches production code (Principles #11, #14).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from boto3.dynamodb.types import TypeDeserializer

from boundary import Call
from row_invariants import FINAL, TOKEN_FIELDS, Answer, Pass, Run, RunTaskExecution, RowWrite, Start, Task

_deserializer = TypeDeserializer()
#: The components that run the dispatcher's passes: the stream's, the sweeper's and AssetFanout's.
PASSERS = frozenset({"lambda:reconcile", "lambda:sweeper", "lambda:asset-fanout"})


def plain(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return {k: _deserializer.deserialize(v) for k, v in item.items()} if item else None


def _moment(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass
class Observer:
    """Records the starts, answers and passes of a World as they happen."""
    read_row: Callable[[str], Optional[Dict[str, Any]]]
    run_rows: Callable[[str], List[Dict[str, Any]]]
    log: List[Call]
    starts: List[Start] = field(default_factory=list)
    answers: List[Answer] = field(default_factory=list)
    passes: List[Pass] = field(default_factory=list)
    #: Each DescribeExecution call of the dispatcher: the source of the pass that made it (``stream``, ``event``,
    #: ``sweeper``, ...; None outside a pass) and the execution's ARN (ADR #137 §1).
    describes: List[Tuple[Optional[str], str]] = field(default_factory=list)
    _sources: List[str] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    #: Each pass in progress over a run, the run's rows as they were when it began: status and generation by key.
    _read_before: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    def install(self, runtime: Any, run_task_arn: str, dispatcher: Any) -> None:
        start, succeed, fail, pass_over = runtime._start, runtime.send_task_success, runtime.send_task_failure, \
            dispatcher._pass
        describe = runtime.describe_execution

        def describe_execution(**kwargs: Any) -> Any:
            self.describes.append((self._sources[-1] if self._sources else None, str(kwargs.get("executionArn"))))
            return describe(**kwargs)

        def _start(machine_arn: str, name: Optional[str], payload: Any) -> Any:
            execution = start(machine_arn, name, payload)
            if machine_arn == run_task_arn and isinstance(payload, dict):
                row = self.read_row(str(payload.get("execution_name"))) or {}
                self.starts.append(Start(str(payload.get("execution_name")), int(payload.get("generation", -1)),
                                         str(row.get("status")), int(row.get("generation", -1))))
            return execution

        def answering(send: Callable[..., Any]) -> Callable[..., Any]:
            def answer(**kwargs: Any) -> Any:
                frame = runtime.tokens.get(kwargs.get("taskToken"))
                if frame is not None and frame.state == "Start_Run":
                    run = frame.execution.name
                    rows = self.run_rows(run)
                    run_row = self.read_row(f"_run_{run}") or {}
                    before = self._read_before.get(run, [{}])[-1]
                    unfinished = {r["execution_name"]: r for r in rows if r.get("status") not in FINAL}
                    raced = sorted(k for k, r in unfinished.items() if before.get(k, (None, 0))[0] in FINAL
                                   and int(r.get("generation", 0)) > before[k][1])
                    self.answers.append(Answer(run, str(kwargs["taskToken"]),
                                               tuple(sorted(set(unfinished) - set(raced))),
                                               stop_mark="stop" in run_row, raced=tuple(raced)))
                return send(**kwargs)
            return answer

        def _pass(run_key: str, targets: Any, **kwargs: Any) -> Any:
            first, answered = len(self.log), len(self.answers)
            run = run_key[len("_run_"):]
            self._read_before.setdefault(run, []).append(
                {str(r["execution_name"]): (r.get("status"), int(r.get("generation", 0))) for r in self.run_rows(run)})
            self._sources.append(str(kwargs.get("source")))
            try:
                result = pass_over(run_key, targets, **kwargs)
            finally:
                self._read_before[run].pop()
                self._sources.pop()
            calls = self.log[first:]
            reads = [c for c in calls if c.operation in ("GetItem", "BatchGetItem", "Query") and not c.ok]
            closes = [c for c in calls if c.ok and _closes_a_run_row(c)]
            self.passes.append(Pass(f"{run_key}#{next(self._ids)}", unresolved_read_failures=len(reads),
                                    starts=int((result or {}).get("started", 0)), closes=len(closes),
                                    answers=len(self.answers) - answered))
            return result

        runtime._start = _start
        runtime.send_task_success, runtime.send_task_failure = answering(succeed), answering(fail)
        runtime.describe_execution = describe_execution
        dispatcher._pass = _pass


@dataclass
class StackView:
    """`RowsView` over a World's stack, as of `now`."""
    now: datetime
    items: List[Dict[str, Any]]
    executions: List[Any]
    run_task_arn: str
    observer: Observer

    def runs(self) -> List[Run]:
        found = []
        by_name = {e.name: e for e in self.executions}
        for item in self.items:
            key = str(item["execution_name"])
            if not key.startswith("_run_") or "#" in key:
                continue
            name = key[len("_run_"):]
            execution = by_name.get(name)
            pauses = item.get("pauses") or []
            found.append(Run(
                id=name, status=str(item.get("status", "")), token=item.get("run_token"),
                answered=item.get("run_token") if item.get("answered_order") is not None
                and item.get("answered_order") == item.get("token_order") else None,
                stop_mark="stop" in item, stop_done="stop_done" in item, request_pending=bool(item.get("requests")),
                paused=bool(pauses) and len(pauses[-1]) == 1,
                due_at=_moment(item.get("attention_at")) if item.get("attention") else None,
                expires_at=_moment(item.get("ttl")),
                execution_ended=execution is not None and execution.status != "RUNNING",
                execution_ended_at=_moment(execution.ended) if execution is not None and execution.ended else None))
        return found

    def tasks(self, run: str) -> List[Task]:
        outputs = {str(i["execution_name"]) for i in self.items if str(i["execution_name"]).startswith("output#")}
        return [Task(key=str(i["execution_name"]), run=run, status=str(i["status"]), generation=int(i["generation"]),
                     token_fields=tuple(f for f in TOKEN_FIELDS if f in i), expires_at=_moment(i.get("ttl")),
                     output_recorded=f"output#{i.get('pipeline_name')}#{i.get('task_name')}#{i.get('date')}" in outputs,
                     waiting_since=_moment(i.get("waiting_since")))
                for i in self.items
                if i.get("pipeline_execution") == run and "generation" in i and "status" in i
                and not str(i["execution_name"]).startswith("_")]

    def run_task_executions(self) -> List[RunTaskExecution]:
        return [RunTaskExecution(str(e.input.get("execution_name")), int(e.input.get("generation", -1)),
                                 e.status == "RUNNING")
                for e in self.executions if e.machine.arn == self.run_task_arn and isinstance(e.input, dict)]

    def starts(self) -> List[Start]:
        return list(self.observer.starts)

    def answers(self) -> List[Answer]:
        return list(self.observer.answers)

    def row_writes(self) -> List[RowWrite]:
        writes = []
        for call in self.observer.log:
            if not call.ok or call.rows_after is None or call.rows_before is None:
                continue
            for key, after in call.rows_after.items():
                before = call.rows_before.get(key)
                if key.startswith("_") or "#" in key or after is None or "status" not in after:
                    continue
                writes.append(RowWrite(
                    kind="run_task" if self._by_run_task(call) else _kind(call, after), task=key,
                    status_before=str(before["status"]) if before and "status" in before else None,
                    status_after=str(after["status"]),
                    generation_before=int(before.get("generation", 0)) if before else 0,
                    generation_after=int(after.get("generation", 0)),
                    conditional=_conditional(call, key), checks_run_version=_checks_run_version(call),
                    writer_generation=_value(call, ":g") if self._by_run_task(call) else None))
        return writes

    def passes(self) -> List[Pass]:
        return list(self.observer.passes)

    def _by_run_task(self, call: Call) -> bool:
        """The interpreter acts as `sfn:{machine}:{execution}` when a state machine writes (sfn_runtime)."""
        return call.component.startswith(f"sfn:{self.run_task_arn.rsplit(':', 1)[-1]}:")


def _kind(call: Call, after: Dict[str, Any]) -> str:
    if call.component in PASSERS:
        return "stop" if after.get("status") == "stopped" else "decision"
    if call.component == "lambda:start-run":
        return "start_run"
    if call.component.startswith("console"):
        return "action"
    return "other"


def _updates(call: Call) -> List[Dict[str, Any]]:
    if call.operation == "TransactWriteItems":
        return [body for item in call.params["TransactItems"] for kind, body in item.items() if kind != "ConditionCheck"]
    return [call.params]


def _key_of(body: Dict[str, Any]) -> str:
    key = (body.get("Key") or body.get("Item") or {}).get("execution_name")
    return str(key["S"] if isinstance(key, dict) else key)


def _conditional(call: Call, key: str) -> bool:
    """The write of `key` is conditioned on the row's status and generation."""
    for body in _updates(call):
        if _key_of(body) == key:
            names = body.get("ExpressionAttributeNames") or {}
            condition = str(body.get("ConditionExpression", ""))
            referenced = {name for alias, name in names.items() if alias in condition}
            return {"status", "generation"} <= referenced
    return False


def _checks_run_version(call: Call) -> bool:
    """The write's transaction checks a run row's run_version."""
    if call.operation != "TransactWriteItems":
        return False
    for item in call.params["TransactItems"]:
        check = item.get("ConditionCheck")
        if check and _key_of(check).startswith("_run_") and "run_version" in (check.get("ExpressionAttributeNames") or {}).values():
            return True
    return False


def _value(call: Call, name: str) -> Optional[int]:
    for body in _updates(call):
        value = (body.get("ExpressionAttributeValues") or {}).get(name)
        if value is not None:
            return int(value["N"] if isinstance(value, dict) else value)
    return None


def _closes_a_run_row(call: Call) -> bool:
    """A write that took a run row's due time away, read from the row before and after it, whatever the
    expression's wording (the review of TR-4)."""
    before, after = call.rows_before or {}, call.rows_after or {}
    return any(key.startswith("_run_") and (before.get(key) or {}).get("attention_at") is not None
               and after.get(key) is not None and "attention_at" not in (after.get(key) or {})
               for key in after)
