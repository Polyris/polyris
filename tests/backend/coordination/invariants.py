"""ADR #130 invariants 1, 3, 4, 5 and 10, checked after every step of every scenario.

The World calls `observe` each time the stack settles, so a scenario that
passes its own assertions still fails when it moved the system through a
state the ADR forbids:

1. a wrapper execution accepts at most one dependency signal;
3. a task row's status never moves backwards under one wrapper execution:
   only a new wrapper (a restart, or another run) may start the row over, not
   the same wrapper registering again after its dependency wait timed out;
4. a waiting row keeps its subscription pointers until its wrapper accepted
   a signal;
5. a reconcilable row without a pause token has a `waiting#` attention entry,
   and a row held in its pause wait a `paused#` one;
10. a run whose task rows have all finished, and none of whose executions is
    still running, has finished too: something told it (ADR #135), or a warning
    in the bell says it could not be told. A run left RUNNING otherwise shows its
    tasks ended in the console while its execution goes on (F16, B1, Z1, 1.2.0).
"""

import json
from typing import Any, Dict, List, Optional, Set, Tuple

from boto3.dynamodb.types import TypeDeserializer

from polyris.coordination import spec as sp
from polyris.coordination.statuses import RECONCILABLE, TERMINAL, WAITING_PAUSED

#: The dependency wait of the dependency wrapper: where its token is signalled.
WAIT_STATE = "Wait_For_Dependencies"

#: How far a task row has moved; a row never goes to a lower rank within one registration.
#: Every status not listed is final for that registration (terminal, stopped, aborted).
#: A `waiting_paused` row with a pause token is held by run_task at its start, past its
#: dependency wait (ADR #130 §2), so it ranks with `deps_ready`; see `_rank`.
_RANK = {"waiting": 0, "waiting_paused": 0, "deps_ready": 1, "waiting_delay": 1, "running": 2,
         "waiting_decision": 3}
_FINAL = 4


def _rank(row: Dict[str, Any]) -> int:
    if row["status"] == WAITING_PAUSED and row.get("pause_token"):
        return _RANK["deps_ready"]
    return _RANK.get(str(row["status"]), _FINAL)

_deserializer = TypeDeserializer()


def _scan(ddb: Any, table: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {"TableName": table, "ConsistentRead": True}
    while True:
        page = ddb.scan(**kwargs)
        items += [{k: _deserializer.deserialize(v) for k, v in i.items()} for i in page.get("Items", [])]
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _is_task_row(item: Dict[str, Any]) -> bool:
    name = str(item.get("execution_name", ""))
    return bool(name) and not name.startswith("_") and "#" not in name and "status" in item


def _pointer_keys(row: Dict[str, Any]) -> Set[str]:
    short = str(row.get("pipeline_execution_short", ""))
    keys = {f"{dep}-{short}" for dep in json.loads(row.get("dependencies") or "[]")}
    return keys | {f"asset:{a}" for a in sp.asset_names(sp.parse(json.loads(row.get("wait_for") or "[]")))}


class Invariants:
    def __init__(self, ddb: Any, runtime: Any, tables: Dict[str, str]):
        self.ddb, self.runtime = ddb, runtime
        self.tokens, self.subscriptions = tables["PipelineTokensTable"], tables["DependencySubscriptionsTable"]
        self.violations: List[str] = []
        #: Runs a warning in the bell says could not be told how a task ended (ADR #135).
        self._told_it_could_not: Set[str] = set()
        #: (execution_name, wrapper_execution_arn) -> the highest rank seen, and its status
        self._seen: Dict[Tuple[str, Optional[str]], Tuple[int, str]] = {}

    def observe(self) -> None:
        signalled = self._accepted_signals()
        pointers = {(str(i["dependency_key"]), str(i["subscriber"])) for i in _scan(self.ddb, self.subscriptions)}
        items = _scan(self.ddb, self.tokens)
        rows = list(filter(_is_task_row, items))
        self._told_it_could_not = {str(i.get("pipeline_execution", "")) for i in items
                                   if str(i.get("execution_name", "")).startswith("_notify_warn_coordination_")
                                   and "stays RUNNING" in str(i.get("error", ""))}
        for row in rows:
            self._never_backwards(row)
            self._pointers_kept(row, pointers, signalled)
            self._in_the_attention_index(row)
        self._runs_end(rows)
        for arn, count in signalled.items():
            if count > 1:
                self._violate(f"invariant 1: wrapper {arn} accepted {count} dependency signals")

    def _accepted_signals(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for callback in self.runtime.callbacks:
            if callback["state"] == WAIT_STATE:
                counts[callback["execution"]] = counts.get(callback["execution"], 0) + 1
        return counts

    def _never_backwards(self, row: Dict[str, Any]) -> None:
        status = str(row["status"])
        key = (str(row["execution_name"]), row.get("wrapper_execution_arn"))
        rank = _rank(row)
        previous = self._seen.get(key)
        if previous is not None and rank < previous[0]:
            self._violate(f"invariant 3: {key[0]} went from {previous[1]} back to {status}")
        if previous is None or rank >= previous[0]:
            self._seen[key] = (rank, status)

    def _pointers_kept(self, row: Dict[str, Any], pointers: Set[Tuple[str, str]], signalled: Dict[str, int]) -> None:
        if row["status"] not in RECONCILABLE or row.get("pause_token"):
            return
        if signalled.get(str(row.get("wrapper_execution_arn", ""))):
            return                                   # the signal was accepted: its pointers may go
        name = str(row["execution_name"])
        missing = sorted(k for k in _pointer_keys(row) if (k, name) not in pointers)
        if missing:
            self._violate(f"invariant 4: {name} is still waiting, but its pointers {missing} are gone")

    def _in_the_attention_index(self, row: Dict[str, Any]) -> None:
        name, status, attention = str(row["execution_name"]), row["status"], str(row.get("attention", ""))
        if status == WAITING_PAUSED and row.get("pause_token"):
            if not attention.startswith("paused#"):
                self._violate(f"invariant 5: {name} is held in its pause wait without a paused# entry")
        elif status in RECONCILABLE and not attention.startswith("waiting#"):
            self._violate(f"invariant 5: {name} is {status} without a waiting# entry")

    def _runs_end(self, rows: List[Dict[str, Any]]) -> None:
        by_run: Dict[str, List[str]] = {}
        for row in rows:
            by_run.setdefault(str(row.get("pipeline_execution", "")), []).append(str(row["status"]))
        working = {str(e.input.get("pipeline_execution", "")) for e in self.runtime.executions.values()
                   if e.status == "RUNNING" and isinstance(e.input, dict)}
        for execution in self.runtime.executions.values():
            statuses = by_run.get(execution.name)
            if (statuses and execution.status == "RUNNING" and all(s in TERMINAL for s in statuses)
                    and execution.name not in working and execution.name not in self._told_it_could_not):
                self._violate(f"invariant 10: run {execution.name} is RUNNING though its tasks finished: {sorted(statuses)}")

    def _violate(self, message: str) -> None:
        if message not in self.violations:
            self.violations.append(message)
