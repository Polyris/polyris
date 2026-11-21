"""The boto3 boundary of the harness: who called DynamoDB, with what, and how it ended (ADR #137, Tests).

Every component the harness runs reaches DynamoDB through one of two shared
clients: the interpreter's, for state-machine integrations, and the one under
the Lambdas' and the console's resource. `Boundary` registers botocore's event
hooks on both, so no production code carries a hook (Principles #11 and #14).

- It logs each call with the component that made it. `acting_as` names the
  component: the bindings run each Lambda handler and the console under their
  names, and the interpreter each integration under its machine and execution.
  A call made outside any component, such as the World's own scans, is not logged.
- `between` runs an action after a read returns and before its caller goes on,
  or before a write is sent, so a scenario can order a race.
- `unprocess` answers a BatchGetItem with part of its keys left unprocessed.
- `watch` records, for each write to one table, the rows it touched as they
  stood before and after it, read by a reader of the World's that no hook sees:
  what ADR #137's invariants call a row's write (rows_view.py).
"""

from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

READS = frozenset({"GetItem", "Query", "Scan", "BatchGetItem", "TransactGetItems"})
WRITES = frozenset({"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"})

_component: ContextVar[Optional[str]] = ContextVar("harness_component", default=None)


@contextmanager
def acting_as(component: str) -> Iterator[None]:
    """Attribute the DynamoDB calls made inside the block to `component`."""
    token = _component.set(component)
    try:
        yield
    finally:
        _component.reset(token)


def as_component(component: str, handler: Callable[[Any, Any], Any]) -> Callable[[Any, Any], Any]:
    """`handler`, run as `component` each time it is called."""
    def run(event: Any, context: Any) -> Any:
        with acting_as(component):
            return handler(event, context)
    return run


@dataclass
class Call:
    component: str
    operation: str
    params: Dict[str, Any]
    sent: bool = False             # it reached DynamoDB; a hook that raises first leaves it unsent
    error: Optional[str] = None    # the error code DynamoDB answered with
    answered: bool = False         # its caller got the answer: a write whose answer is lost was sent, not answered
    rows_before: Optional[Dict[str, Optional[Dict[str, Any]]]] = None   # the watched rows, before a write
    rows_after: Optional[Dict[str, Optional[Dict[str, Any]]]] = None    # and after it, when it went in

    @property
    def ok(self) -> bool:
        return self.sent and self.error is None and self.answered

    @property
    def tables(self) -> List[str]:
        if "TableName" in self.params:
            return [self.params["TableName"]]
        if "RequestItems" in self.params:
            return list(self.params["RequestItems"])
        names = []
        for item in self.params.get("TransactItems", []):
            for body in item.values():
                if body.get("TableName") not in names:
                    names.append(body.get("TableName"))
        return names


@dataclass
class _Interleave:
    phase: str                               # "after_read" or "before_write"
    matches: Callable[[Call], bool]
    then: Callable[[], None]
    left: int


@dataclass
class _Unprocess:
    table: str
    keys: Sequence[str]
    keep: int
    left: int


@dataclass
class Boundary:
    clients: Sequence[Any]
    log: List[Call] = field(default_factory=list)
    _interleaves: List[_Interleave] = field(default_factory=list)
    _unprocess: List[_Unprocess] = field(default_factory=list)
    _watched: Optional[str] = None
    _read_row: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None

    def __post_init__(self) -> None:
        # Per operation: botocore calls the hooks of a more specific event first, and the World's own
        # fault injections hook each operation. Ours log first, and mark the answer last.
        for client in self.clients:
            for operation in READS | WRITES:
                client.meta.events.register_first(f"before-parameter-build.dynamodb.{operation}", self._before)
                client.meta.events.register_first(f"after-call.dynamodb.{operation}", self._sent)
                client.meta.events.register_last(f"after-call.dynamodb.{operation}", self._answered)

    # ── what scenarios ask for ─────────────────────────────────────────

    def between(self, *, then: Callable[[], None], after_read: Optional[Callable[[Call], bool]] = None,
                before_write: Optional[Callable[[Call], bool]] = None, times: int = 1) -> None:
        """Run `then` after a matching read returns, or before a matching write is sent, `times` times."""
        if (after_read is None) == (before_write is None):
            raise ValueError("give exactly one of after_read and before_write")
        phase, matches = ("after_read", after_read) if after_read is not None else ("before_write", before_write)
        assert matches is not None
        self._interleaves.append(_Interleave(phase, matches, then, times))

    def unprocess(self, *, table: str, keys: Sequence[str], keep: int, times: int = 1) -> None:
        """Answer the next `times` BatchGetItems on `table` with only `keep` of its items; the rest come
        back as UnprocessedKeys, as DynamoDB answers a batch it throttled or cut at 16 MB."""
        self._unprocess.append(_Unprocess(table, keys, keep, times))

    def heal(self) -> None:
        """The interleavings and cut batches a scenario arranged and that have not happened go (World.run_to_the_end)."""
        self._interleaves.clear()
        self._unprocess.clear()

    def watch(self, table: str, read_row: Callable[[str], Optional[Dict[str, Any]]]) -> None:
        """Record the rows each write to `table` touches, before and after; `read_row` reads one by its key."""
        self._watched, self._read_row = table, read_row

    def calls(self, *, component: Optional[str] = None, operations: Optional[frozenset] = None) -> List[Call]:
        return [c for c in self.log if (component is None or c.component == component)
                and (operations is None or c.operation in operations)]

    # ── the hooks ──────────────────────────────────────────────────────

    def _before(self, params: Dict[str, Any], model: Any, context: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        component = _component.get()
        if component is None or context is None:
            return
        call = Call(component, model.name, copy.deepcopy(params))
        self.log.append(call)
        context["harness_call"] = call
        if call.operation in WRITES:
            self._interleave("before_write", call)
            keys = self._watched_keys(call)
            if keys:
                call.rows_before = {key: self._read_row(key) for key in keys}   # type: ignore[misc]

    def _sent(self, parsed: Dict[str, Any], context: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        call = context.get("harness_call") if context is not None else None
        if call is not None:
            call.sent = True
            call.error = ((parsed or {}).get("Error") or {}).get("Code") or None

    def _answered(self, parsed: Dict[str, Any], context: Optional[Dict[str, Any]] = None, **_: Any) -> None:
        """The last hook: reached only when no other hook turned the answer into an error."""
        call = context.get("harness_call") if context is not None else None
        if call is None:
            return
        call.answered = True
        if not call.ok:
            return
        if call.rows_before is not None:
            call.rows_after = {key: self._read_row(key) for key in call.rows_before}   # type: ignore[misc]
        if call.operation == "BatchGetItem":
            self._cut(parsed)
        if call.operation in READS:
            self._interleave("after_read", call)

    def _watched_keys(self, call: Call) -> List[str]:
        """The keys of the watched table's rows a write touches; a transaction's ConditionCheck touches none."""
        if self._watched is None or self._watched not in call.tables:
            return []
        params, keys = call.params, []
        bodies: List[Dict[str, Any]] = []
        if call.operation == "TransactWriteItems":
            bodies = [body for item in params["TransactItems"] for kind, body in item.items() if kind != "ConditionCheck"]
        elif call.operation == "BatchWriteItem":
            bodies = [{"TableName": self._watched, **next(iter(r.values()))}
                      for r in params["RequestItems"].get(self._watched, [])]
        else:
            bodies = [params]
        for body in bodies:
            if body.get("TableName") == self._watched:
                key = (body.get("Key") or body.get("Item") or {}).get("execution_name")
                keys.append(str(key["S"] if isinstance(key, dict) else key))
        return keys

    def _interleave(self, phase: str, call: Call) -> None:
        for step in self._interleaves:
            if step.phase == phase and step.left > 0 and step.matches(call):
                step.left -= 1               # before `then`, whose own calls pass through here too
                step.then()

    def _cut(self, parsed: Dict[str, Any]) -> None:
        for cut in self._unprocess:
            items = parsed.get("Responses", {}).get(cut.table)
            if cut.left <= 0 or items is None or len(items) <= cut.keep:
                continue
            cut.left -= 1
            kept, held = items[:cut.keep], items[cut.keep:]
            parsed["Responses"][cut.table] = kept
            pending = parsed.setdefault("UnprocessedKeys", {}).setdefault(cut.table, {"Keys": []})
            pending["Keys"] += [{k: item[k] for k in cut.keys} for item in held]
