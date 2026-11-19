"""The dispatcher (ADR #137 §1, §4, §7): passes over the rows of a run, and what they decide.

A pass reads the run row, then the rows of its set by key, with consistent reads
and no index. It decides each row with the pure core
(``polyris.coordination.dispatch``) and sends what that decides
(``dispatch_writes``), one row at a time, upstreams first: a row decided in a
pass counts for its dependants in the same pass once its write is in. A stop
goes the other way, dependants first (§4).

- A full pass covers every row of the run, then decides the run's end: the
  summary, the answer of its token and its mark, the stop done, and the removal
  of the run row's due time (§4, §3).
- A narrow pass covers the rows a fact wakes and their direct upstreams; it
  leaves the run's end to the next full pass (§7).
- A refused write is decided again once from a new read of the run row, the row
  and its upstreams; a ``TransactionConflict`` is tried again after a short
  random backoff. Every pass has a time budget: a pass that leaves rows makes its
  run row due at once and sets ``left_at``, which wakes the next pass (§7, Rule 52).
- A transient AWS error raises ``CoordinationTransient``: the stream or the
  caller tries again. Anything else ends in a row or in a warning, so one bad run
  never holds up a shard (Rules 51, 52). An ``AccessDeniedException`` fails no
  row: it warns, and the row waits for its next check (§7).

Entry points: ``handler`` for the tasks table's stream, ``full_pass`` for the
sweeper, ``narrow_pass`` for AssetFanout.
"""

import random
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

import env
import failures
from failures import CoordinationTransient
from logger import log
from polyris.constants import TaskSpecKey, TaskStatus, WarningKind
from polyris.coordination import asset_spec, dispatch, dispatch_writes, records, run_input, run_row, run_task_input
from polyris.coordination import spec_store, task_row, task_spec, transitions
from polyris.coordination.asset_conditions import Evaluation
from polyris.coordination.asset_conditions import evaluate as evaluate_assets
from polyris.coordination.partition import partition_date
from stores import FactStore, Registry

S = TaskStatus
#: Time a pass keeps in hand before its caller's timeout (Rule 52).
RESERVE_MS = 5_000
#: A write that a TransactionConflict cancelled is sent again after a short random backoff (§7).
CONFLICT_ATTEMPTS = 3
CONFLICT_BACKOFF_SECONDS = (0.05, 0.25)
#: A batch read is sent again with what DynamoDB left unprocessed, this many times.
READ_ATTEMPTS = 8
READ_BATCH = 100
#: The spec versions a container keeps by spec_id: a version never changes (§3).
SPEC_CACHE = 64
ACCESS_DENIED = frozenset({"AccessDeniedException", "AccessDenied"})
#: The cancellation reason of a transaction that met another one (§7).
CONFLICT = "TransactionConflict"

_deserializer = TypeDeserializer()
_serializer = TypeSerializer()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(item: Mapping[str, Any]) -> Dict[str, Any]:
    return {k: _deserializer.deserialize(v) for k, v in item.items()}


class _Left(Exception):
    """The pass's budget ran out; the rest of its set is left to the next pass (§7)."""


@dataclass
class _Pass:
    """What one pass reads and counts."""
    run: dispatch.RunView
    items: Dict[str, Dict[str, Any]]
    rows: Dict[str, dispatch.RowView]
    specs: Mapping[str, task_spec.TaskSpec]
    deadline: float
    source: str
    now: datetime
    changed: int = 0
    started: int = 0
    left: bool = False
    inputs: Optional[run_input.RunInput] = None


class Dispatcher:
    def __init__(self, client: Any, sfn: Any, *, resource: Any, tokens_table: str, subscriptions_table: str,
                 specs_table: str, asset_events_table: str, pipelines_table: str, run_task_arn: str,
                 clock: Callable[[], datetime] = _utc_now, monotonic: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.sfn = sfn
        self.resource = resource
        self.tokens_table = tokens_table
        self.subscriptions_table = subscriptions_table
        self.specs_table = specs_table
        self.asset_events_table = asset_events_table
        self.pipelines_table = pipelines_table
        self.run_task_arn = run_task_arn
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._specs: Dict[str, Dict[str, task_spec.TaskSpec]] = {}

    # ── entry points ─────────────────────────────────────────────────────

    def full_pass(self, run_key: str, *, source: str, remaining_ms: Callable[[], int]) -> Dict[str, Any]:
        """A pass over every row of the run, which also decides the run's end (§4)."""
        return self._pass(run_key, None, source=source, remaining_ms=remaining_ms)

    def narrow_pass(self, run_key: str, task_keys: Sequence[str], *, source: str,
                    remaining_ms: Callable[[], int]) -> Dict[str, Any]:
        """A pass over the rows ``task_keys`` name and their direct upstreams (§7)."""
        return self._pass(run_key, set(task_keys), source=source, remaining_ms=remaining_ms)

    def handle(self, records_: List[Dict[str, Any]], remaining_ms: Callable[[], int]) -> Dict[str, Any]:
        """A batch of the tasks table's stream: one full pass per run whose change wakes it (§7).

        A record that the filters let through and the transition table does not
        mark is dropped. On a transient failure, or when the budget runs out, the
        batch is redelivered from the first record of the run not passed over.
        """
        runs: Dict[str, List[Dict[str, Any]]] = {}
        orphans: Dict[str, Set[str]] = {}
        for record in records_:
            key, old, new = _images(record)
            if not transitions.wakes(key, old, new):
                continue
            image = new or old or {}
            if run_row.is_run_row(key):
                runs.setdefault(key, []).append(record)
            elif image.get(task_row.PIPELINE_EXECUTION):
                run_key = run_row.key(str(image[task_row.PIPELINE_EXECUTION]))
                runs.setdefault(run_key, []).append(record)
                orphans.setdefault(run_key, set()).add(key)
        for index, (run_key, run_records) in enumerate(runs.items()):
            if remaining_ms() < RESERVE_MS:
                return _redeliver(list(runs.values())[index:])
            try:
                self._pass(run_key, None, source="stream", remaining_ms=remaining_ms,
                           orphan_keys=orphans.get(run_key, set()))
            except CoordinationTransient as exc:
                log.error("dispatcher", "a transient failure; the batch is redelivered from this run",
                          run=run_key, error=str(exc))
                return _redeliver(list(runs.values())[index:])
        return {"batchItemFailures": []}

    # ── a pass ───────────────────────────────────────────────────────────

    def _pass(self, run_key: str, targets: Optional[Set[str]], *, source: str, remaining_ms: Callable[[], int],
              orphan_keys: Optional[Set[str]] = None) -> Dict[str, Any]:
        deadline = self.monotonic() + max(0, remaining_ms() - RESERVE_MS) / 1000
        try:
            return self._run_pass(run_key, targets, source=source, deadline=deadline,
                                  orphan_keys=orphan_keys or set(targets or ()))
        except CoordinationTransient:
            raise
        except (ClientError, BotoCoreError) as exc:
            failures.reraise_transient(exc)
            return self._failed(run_key, source, exc)
        except Exception as exc:  # noqa: BLE001 — one bad run never holds up its shard (Rules 51, 52)
            return self._failed(run_key, source, exc)

    def _run_pass(self, run_key: str, targets: Optional[Set[str]], *, source: str, deadline: float,
                  orphan_keys: Set[str]) -> Dict[str, Any]:
        now = self.clock()
        run_item = self._get(run_key)
        if run_item is None:
            return self._orphans(run_key, sorted(orphan_keys), source=source, now=now)
        run = dispatch.run_view(run_item)
        if not run.active and not run.stopping:
            return {"run": run_key, "source": source, "action": "creating"}    # nothing starts before (§1)
        specs = self._spec_version(run_item)
        keys = {name: task_row.key(name, run.date, run.execution) for name in specs}
        if targets is None:
            judged = set(specs)
            wanted = set(specs)
        else:
            judged = {name for name, key in keys.items() if key in targets}
            wanted = judged | {d for name in judged for d in specs[name].fields[TaskSpecKey.DEPENDENCIES]}
        state = _Pass(run, {}, {}, specs, deadline, source, now)
        self._read_rows(state, {name: keys[name] for name in wanted})
        try:
            if run.stopping:
                for name in dispatch.stop_order({n: r for n, r in state.rows.items() if n in judged}):
                    self._row(state, name, stop=True)
            else:
                for name in _upstreams_first(judged, specs):
                    if name in state.rows:
                        self._row(state, name)
            if targets is None:
                self._run_end(state, keys)
        except _Left:
            state.left = True
        if state.left:
            self._send(dispatch_writes.due_now(state.run, table=self.tokens_table, now=now,
                                               left=bool(state.changed) or state.run.left_at is None), quiet=True)
        result = {"run": run_key, "source": source, "rows": len(state.rows), "changed": state.changed,
                  "started": state.started, "left": state.left}
        log.info("dispatcher", "pass", **result)
        return result

    def _row(self, state: _Pass, name: str, *, stop: bool = False) -> None:
        """Decide one row and send what was decided; decide it again once from new reads if refused."""
        for attempt in range(2):
            if self.monotonic() > state.deadline:
                raise _Left()
            row = state.rows[name]
            judgment = self._judge(state, name, stop=stop)
            if self._write(state, row, judgment):
                self._after(state, name, row, judgment)
                return
            if attempt == 0:
                self._read_again(state, name)
        state.left = True                                          # refused twice: the next pass takes it (§7)

    def _judge(self, state: _Pass, name: str, *, stop: bool) -> dispatch.Judgment:
        row = state.rows[name]
        if stop:
            return dispatch.stop(row, now=state.now, cause=_stop_cause(state.run))
        upstreams = {d: state.rows.get(d) for d in row.dependencies}
        assets = self._assets(state, name) if dispatch.needs_assets(row, upstreams) else None
        return dispatch.judge(row, upstreams, state.run, now=state.now, assets=assets)

    def _after(self, state: _Pass, name: str, row: dispatch.RowView, judgment: dispatch.Judgment) -> None:
        """What follows a write that went in: the row as dependants now read it, its pointers, a warning, a start."""
        if judgment.transition or judgment.fields or judgment.removed or judgment.due != row.due:
            state.changed += 1
            log.info("dispatcher", "row", run=state.run.key, task=name, status=judgment.status or row.status,
                     transition=judgment.transition, cause=judgment.cause)
        if judgment.transition:
            target = str(judgment.status)
            final = target in transitions.FINAL
            state.rows[name] = replace(
                row, status=target, times=replace(row.times, status=target),
                generation=row.generation + 1 if judgment.transition == "stop" else row.generation,
                skip_origin=judgment.fields.get(task_row.SKIP_ORIGIN, row.skip_origin),
                finished_at=state.now if final else row.finished_at)
        if judgment.leaves_wait and row.has_wait_for:
            self._drop_pointers(state.items[name])
        if judgment.warning:
            self._warn(state.run, subject=row.key, task=name, item=state.items[name], error=judgment.warning)
        if judgment.start:
            self._start(state, name, row)

    def _read_again(self, state: _Pass, name: str) -> None:
        """A refused write: the run row, the row and its upstreams read again (§1)."""
        run_item = self._get(state.run.key)
        if run_item is None:
            raise _Left()
        state.run = dispatch.run_view(run_item)
        row = state.rows[name]
        keys = {n: task_row.key(n, state.run.date, state.run.execution) for n in (name, *row.dependencies)}
        self._read_rows(state, keys)
        if name not in state.rows:
            raise _Left()

    # ── the run's end ────────────────────────────────────────────────────

    def _run_end(self, state: _Pass, keys: Mapping[str, str]) -> None:
        """The summary, the answer, the stop done and the run row's due time (§3, §4)."""
        statuses = {name: (state.rows[name].status if name in state.rows else None) for name in keys}
        end = dispatch.run_outcome(state.run, statuses)
        run = state.run
        if end.missing:
            self._warn(run, subject=f"{run.key}-missing", task="", item={},
                       error=f"rows of the run are missing, and count as failed: {', '.join(end.missing)}")
        if dispatch.summary_changed(run, end):
            self._send(dispatch_writes.summary(run, end, table=self.tokens_table, read_at=state.now), quiet=True)
        if dispatch.needs_answer(run, end) and self._answer(run, end):
            if self._send(dispatch_writes.answered(run, table=self.tokens_table, now=state.now)):
                run = replace(run, version=run.version + 1, answered_order=run.token_order)
        if end.finished and run.answered:
            if run.stopping and self._send(dispatch_writes.stop_done(run, table=self.tokens_table, now=state.now)):
                run = replace(run, version=run.version + 1, stop_done=True)
            if not run.stopping and not run.requests:
                self._send(dispatch_writes.close(run, table=self.tokens_table), quiet=True)
        state.run = run

    def _answer(self, run: dispatch.RunView, end: dispatch.RunEnd) -> bool:
        """Answer the run's token; a closed token counts as answered (§4)."""
        call, arguments = dispatch_writes.answer(run, end)
        try:
            getattr(self.sfn, call)(**arguments)
            log.info("dispatcher", "run answered", run=run.key, outcome=end.outcome)
            return True
        except ClientError as exc:
            if failures.is_dead_token(exc):
                return True
            failures.reraise_transient(exc)
            self._warn(run, subject=f"{run.key}-answer", task="", item={},
                       error=f"the run's token could not be answered: {failures.error_code(exc)}: {exc}")
            return False

    # ── a start ──────────────────────────────────────────────────────────

    def _start(self, state: _Pass, name: str, row: dispatch.RowView) -> None:
        """Start the row's run_task under its derived name (invariant 4); a failure is written on the row."""
        item = state.items[name]
        started = state.rows[name]
        failure = self._start_failure(state, name, item, started)
        if failure is None:
            state.started += 1
            return
        if self._write(state, started, failure):
            self._after(state, name, started, replace(failure, start=False))
        else:
            state.left = True

    def _start_failure(self, state: _Pass, name: str, item: Mapping[str, Any],
                       row: dispatch.RowView) -> Optional[dispatch.Judgment]:
        run = state.run
        if state.inputs is None:
            stored = self._get(run_row.input_key(run.execution))
            if stored is None:
                raise ValueError(f"{run.key}: the run's input item is missing")
            state.inputs = run_input.from_item(stored)
        upstreams = {d: (state.rows[d].status, state.rows[d].skip_origin) for d in row.dependencies}
        try:
            text = run_task_input.build(
                execution_name=row.key, generation=row.generation, task_name=name,
                pipeline_name=str(item.get(task_row.PIPELINE, "")), pipeline_execution=run.execution,
                date=run.date, triggered_by=str(item.get(task_row.TRIGGERED_BY) or "unknown"),
                backfill_id=item.get(task_row.BACKFILL_ID), spec=state.specs[name],
                variables=state.inputs.variables, cascade=state.inputs.cascade, upstreams=upstreams)
        except run_task_input.InputTooLarge as exc:
            return dispatch.start_failed(row, run, now=state.now, error=str(exc), kind=dispatch.REFUSED)
        try:
            self.sfn.start_execution(**dispatch_writes.start_request(row, run, state_machine=self.run_task_arn,
                                                                     input_text=text))
            return None
        except ClientError as exc:
            code = failures.error_code(exc)
            if code == "ExecutionAlreadyExists":
                return None                                   # this start was made already (invariant 4)
            kind = (dispatch.DENIED if code in ACCESS_DENIED
                    else dispatch.TRANSIENT if failures.is_transient(exc) else dispatch.REFUSED)
            return dispatch.start_failed(row, run, now=state.now, error=f"{code}: {exc}", kind=kind)
        except BotoCoreError as exc:
            return dispatch.start_failed(row, run, now=state.now, error=f"{type(exc).__name__}: {exc}",
                                         kind=dispatch.TRANSIENT)

    # ── rows whose run row is missing ────────────────────────────────────

    def _orphans(self, run_key: str, keys: Sequence[str], *, source: str, now: datetime) -> Dict[str, Any]:
        """Each row a change woke, stopped with the cause: its run row is missing (invariant 11)."""
        stopped = 0
        for key in keys:
            item = self._get(key)
            if item is None or task_row.DECISION not in item:
                continue
            row = dispatch.row_view(item)
            judgment = dispatch.stop(row, now=now, cause="its run row is missing")
            items = dispatch_writes.task_write(row, judgment, None, table=self.tokens_table, now=now)
            if items and self._send_items(items):
                stopped += 1
        result = {"run": run_key, "source": source, "action": "orphans", "stopped": stopped}
        log.info("dispatcher", "pass", **result)
        return result

    # ── reads ────────────────────────────────────────────────────────────

    def _spec_version(self, run_item: Mapping[str, Any]) -> Dict[str, task_spec.TaskSpec]:
        spec_id = str(run_item[run_row.SPEC_ID])
        cached = self._specs.get(spec_id)
        if cached is None:
            cached = spec_store.read(self.client, self.specs_table, spec_id,
                                     expected_count=int(run_item[run_row.TASK_COUNT]))
            if len(self._specs) >= SPEC_CACHE:
                self._specs.clear()
            self._specs[spec_id] = cached
        return cached

    def _get(self, key: str) -> Optional[Dict[str, Any]]:
        response = self.client.get_item(TableName=self.tokens_table, Key={task_row.TABLE_KEY: {"S": key}},
                                        ConsistentRead=True)
        item = response.get("Item")
        return _plain(item) if item else None

    def _read_rows(self, state: _Pass, keys: Mapping[str, str]) -> None:
        """The rows by key, consistently; a row of another run under the key counts as missing (invariant 11)."""
        by_key = {key: name for name, key in keys.items()}
        found: Dict[str, Dict[str, Any]] = {}
        names = sorted(by_key)
        for start in range(0, len(names), READ_BATCH):
            request: Dict[str, Any] = {self.tokens_table: {
                "Keys": [{task_row.TABLE_KEY: {"S": k}} for k in names[start:start + READ_BATCH]],
                "ConsistentRead": True}}
            for attempt in range(READ_ATTEMPTS):
                response = self.client.batch_get_item(RequestItems=request)
                for raw in response.get("Responses", {}).get(self.tokens_table, []):
                    item = _plain(raw)
                    found[str(item[task_row.TABLE_KEY])] = item
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
                self.sleep(min(2.0, 0.05 * (2 ** attempt)))
            else:
                raise CoordinationTransient(f"{self.tokens_table}: DynamoDB left keys unread after {READ_ATTEMPTS} "
                                            "attempts")
        for key, name in by_key.items():
            read = found.get(key)
            if read is not None and task_row.of_run(read, state.run.execution, state.run.run_start):
                state.items[name] = read
                state.rows[name] = dispatch.row_view(read)
            else:
                state.items.pop(name, None)
                state.rows.pop(name, None)

    def _assets(self, state: _Pass, name: str) -> Evaluation:
        """The evaluation of the row's facts, read as AssetFanout's reconciler read them (ADR #130 §4)."""
        wait_for = task_spec.decision_from_text(str(state.items[name][task_row.DECISION])).get(
            TaskSpecKey.WAIT_FOR.value)
        registry = Registry(self.resource, self.pipelines_table)
        reader = FactStore(self.resource, self.asset_events_table, registry, state.now,
                           on_unreadable=lambda item, exc: self._unreadable_fact(state.run, item, exc))
        return evaluate_assets(asset_spec.parse(wait_for), target=partition_date(state.run.date), now=state.now,
                               reader=reader)

    # ── writes ───────────────────────────────────────────────────────────

    def _write(self, state: _Pass, row: dispatch.RowView, judgment: dispatch.Judgment) -> bool:
        """Send a row's write; False when a condition refused it."""
        items = dispatch_writes.task_write(row, judgment, state.run, table=self.tokens_table, now=state.now)
        return True if not items else self._send_items(items)

    def _send_items(self, items: List[Dict[str, Any]]) -> bool:
        for attempt in range(CONFLICT_ATTEMPTS):
            try:
                if len(items) == 1:
                    self.client.update_item(**items[0]["Update"])
                else:
                    self.client.transact_write_items(TransactItems=items)
                return True
            except ClientError as exc:
                if failures.is_condition_failed(exc):
                    return False
                if failures.error_code(exc) == "TransactionCanceledException":
                    reasons = [str(r.get("Code", "")) for r in exc.response.get("CancellationReasons", [])]
                    if CONFLICT in reasons and attempt + 1 < CONFLICT_ATTEMPTS:
                        self.sleep(random.uniform(*CONFLICT_BACKOFF_SECONDS))
                        continue
                    if CONFLICT not in reasons and not any(r == "ThrottlingError" for r in reasons):
                        return False                           # a condition: decided again from new reads
                failures.reraise_transient(exc)
                raise
        return False

    def _send(self, update: Dict[str, Any], *, quiet: bool = False) -> bool:
        """A write of the run row; a refused condition is no error (another pass or an action came first)."""
        try:
            self.client.update_item(**update)
            return True
        except ClientError as exc:
            if failures.is_condition_failed(exc):
                return False
            failures.reraise_transient(exc)
            if quiet:
                log.error("dispatcher", "a write of the run row failed", key=update["Key"], error=str(exc))
                return False
            raise

    def _drop_pointers(self, item: Mapping[str, Any]) -> None:
        """The row left the wait, so its asset: pointers go (Rule 40); a stale one costs a no-op pass."""
        for pointer in dispatch_writes.pointers(item):
            try:
                self.client.delete_item(TableName=self.subscriptions_table,
                                        Key={k: {"S": v} for k, v in pointer.items()})
            except (ClientError, BotoCoreError) as exc:
                log.error("dispatcher", "an asset pointer could not be deleted", pointer=pointer, error=str(exc))

    def _warn(self, run: dispatch.RunView, *, subject: str, task: str, item: Mapping[str, Any], error: str) -> None:
        warning = records.warning_item(
            kind=WarningKind.COORDINATION, execution_name=subject, task_name=task,
            pipeline_name=str(item.get(task_row.PIPELINE, "")), pipeline_execution=run.execution,
            task_date=run.date, error=error, now=self.clock())
        log.error("dispatcher", "warning", run=run.key, subject=subject, error=error)
        try:
            self.client.put_item(TableName=self.tokens_table,
                                 Item={k: _serializer.serialize(v) for k, v in warning.items()})
        except (ClientError, BotoCoreError) as exc:
            log.error("dispatcher", "could not write the warning row", subject=subject, error=str(exc))

    def _unreadable_fact(self, run: dispatch.RunView, item: Mapping[str, Any], exc: Exception) -> None:
        asset, recorded = str(item.get("asset_name", "")), str(item.get("event_time", ""))
        self._warn(run, subject=f"fact-{asset}-{recorded}", task="", item={},
                   error=f"The fact of asset {asset} recorded at {recorded} cannot be read, so it counts for no task: "
                         f"{exc}")

    def _failed(self, run_key: str, source: str, exc: BaseException) -> Dict[str, Any]:
        """A pass that failed for good: a warning for its run; its run row's check comes back to it (§7)."""
        run = dispatch.RunView(key=run_key, execution=run_key[len(run_row.KEY_PREFIX):], status="", version=0)
        self._warn(run, subject=f"{run_key}-pass", task="", item={},
                   error=f"a pass over the run failed: {type(exc).__name__}: {exc}")
        return {"run": run_key, "source": source, "action": "failed", "error": f"{type(exc).__name__}: {exc}"}


def _stop_cause(run: dispatch.RunView) -> str:
    stop = run.stop or {}
    reason = str(stop.get("reason") or "no reason given")
    return f"the run was stopped by {stop.get('by') or 'the engine'}: {reason}"


def _upstreams_first(names: Set[str], specs: Mapping[str, task_spec.TaskSpec]) -> List[str]:
    """``names`` in an order where each comes after its upstreams among them (the specs form a DAG)."""
    order: List[str] = []
    seen: Set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        for upstream in sorted(specs[name].fields[TaskSpecKey.DEPENDENCIES]):
            if upstream in names:
                visit(upstream)
        order.append(name)

    for name in sorted(names):
        visit(name)
    return order


def _images(record: Mapping[str, Any]):
    data = record.get("dynamodb", {})
    key = str(_plain(data.get("Keys", {})).get(task_row.TABLE_KEY, ""))
    old, new = data.get("OldImage"), data.get("NewImage")
    return key, (_plain(old) if old else None), (_plain(new) if new else None)


def _redeliver(groups: List[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The stream redelivers from the lowest sequence number reported, so every record after it comes again."""
    numbers = [int(r["dynamodb"]["SequenceNumber"]) for group in groups for r in group]
    return {"batchItemFailures": [{"itemIdentifier": str(min(numbers))}]} if numbers else {"batchItemFailures": []}


_DISPATCHER: Optional[Dispatcher] = None


def from_env() -> Dispatcher:
    """A dispatcher over the function's environment: its tables and the run_task state machine."""
    return Dispatcher(env.dynamodb_client(), env.stepfunctions(), resource=env.dynamodb(),
                      tokens_table=env.TOKENS_TABLE, subscriptions_table=env.SUBSCRIPTIONS_TABLE,
                      specs_table=env.SPECS_TABLE, asset_events_table=env.ASSET_EVENTS_TABLE,
                      pipelines_table=env.PIPELINES_TABLE, run_task_arn=env.RUN_TASK_ARN)


def _dispatcher() -> Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = from_env()
    return _DISPATCHER


def handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    """The tasks table's stream (§7)."""
    remaining: Callable[[], int] = context.get_remaining_time_in_millis if context else (lambda: 10 ** 9)
    return _dispatcher().handle(list(event.get("Records", [])), remaining)
