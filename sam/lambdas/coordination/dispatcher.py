"""The dispatcher (ADR #137 §1, §4, §7): passes over the rows of a run, and what they decide.

A pass reads the run row, then the rows of its set by key, with consistent reads
and no index. It decides each row with the pure core
(``polyris.coordination.dispatch``) and sends what that decides
(``dispatch_writes``), one row at a time, upstreams first: a row decided in a
pass counts for its dependants in the same pass once its write is in. A stop
goes the other way, dependants first (§4).

- A full pass covers every row of the run, then decides the run's end: the
  summary, the answer of its token and its mark, the stop done, and the removal
  of the run row's due time (§4, §3). Before any decision it carries out the
  run's requests through ``coordination.actions``: a Restart's renewal (the
  pointers of each waiting row, then the TTLs) and the reset of a Restart with
  dependants, each cleared once done (§3, §5).
- A narrow pass covers the rows a fact wakes and their direct upstreams; it
  leaves the run's end to the next full pass (§7).
- A refused write is decided again once from a new read of the run row, the row
  and its upstreams; a ``TransactionConflict``, of a transaction or of a single
  write that met run_task's, is tried again after a short random backoff. Every pass has a time budget, spent on what takes time: the
  reads of a row's facts, its writes and its start. Deciding a row is pure, so a
  pass moves past the rows that need nothing to the ones that do. A pass that
  leaves rows makes its run row due at once and sets ``left_at``, which wakes the
  next pass (§7, Rule 52).
- A transient AWS error raises ``CoordinationTransient``: the stream or the
  caller tries again. Anything else ends in a row or in a warning, so one bad run
  never holds up a shard (Rules 51, 52). An ``AccessDeniedException`` fails no
  row: it warns, and the row waits for its next check (§7).
- The sweeper's passes alone (``full_pass(check=True)``) judge executions through
  DescribeExecution (§1, §4): the ``run_task`` of a ``deps_ready`` or ``running``
  row whose liveness check has come, and the run's own execution, which ended
  before its answer or never made the run active; the engine then stops the run.
  They also report the rows whose wait ended on a lost hint (§7).
- A row whose run row is missing is stopped with the cause (invariant 11), a row
  that a 1.2 run left in flight included (*Migration*).

Entry points: ``handler`` for the tasks table's stream, ``full_pass`` for the
sweeper, ``narrow_pass`` for AssetFanout, ``stop_orphans`` for the sweeper's
rows whose run row is missing.
"""

import json
import random
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import BotoCoreError, ClientError

import env
import failures
from failures import CoordinationTransient
from logger import log
from polyris.constants import TaskSpecKey, TaskStatus, WarningKind
from polyris.coordination import actions, asset_spec, dispatch, dispatch_writes, pauses, records, run_input, run_row
from polyris.coordination import run_name, run_task_input, spec_store, sweep, task_row, task_spec, transitions
from polyris.coordination.times import format_utc
from polyris.coordination.asset_conditions import Evaluation
from polyris.coordination.asset_conditions import evaluate as evaluate_assets
from polyris.coordination.partition import partition_date
from stores import FactStore, Registry

S = TaskStatus
#: The rows whose run_task may be running: a stop of one stops that execution too (§4).
_STARTED = frozenset({S.DEPS_READY.value, S.RUNNING.value})
#: The error with which a stop ends an execution, the run's own or a row's run_task.
STOPPED_ERROR = "Polyris.Stopped"
#: Time a pass keeps in hand before its caller's timeout (Rule 52).
RESERVE_MS = 5_000
#: A write that a TransactionConflict cancelled is sent again after a short random backoff (§7).
CONFLICT_ATTEMPTS = 3
CONFLICT_BACKOFF_SECONDS = (0.05, 0.25)
#: A batch read is sent again with what DynamoDB left unprocessed, this many times.
READ_ATTEMPTS = 8
READ_BATCH = 100
#: The spec versions a container keeps by spec_id: a version never changes (§3).
#: The task names a container keeps across invocations, by spec version: a pass keeps no spec text (D117).
NAMES_CACHE = 20_000
#: The bytes of spec texts a container keeps across invocations; a start reads its task's text by hash (D117).
TEXTS_CACHE_BYTES = 32 * 1024 * 1024
ACCESS_DENIED = frozenset({"AccessDeniedException", "AccessDenied"})
#: The cancellation reason of a transaction that met another one (§7).
CONFLICT = "TransactionConflict"
#: What DescribeExecution answers for an execution it does not know.
NO_EXECUTION = "ExecutionDoesNotExist"

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
    names: Mapping[str, str]               # the run's task ids and their spec texts' hashes (D117)
    deadline: float
    source: str
    now: datetime
    changed: int = 0
    started: int = 0
    left: bool = False
    inputs: Optional[run_input.RunInput] = None
    check: bool = False                    # the sweeper's pass: liveness, the engine's stop, lost hints (§7)
    left_read: bool = False                # the run row carried left_at when the pass read it
    lost: List[str] = field(default_factory=list)
    facts: Dict[str, Evaluation] = field(default_factory=dict)    # one evaluation per wait_for a pass


class Dispatcher:
    def __init__(self, client: Any, sfn: Any, *, resource: Any, tokens_table: str, subscriptions_table: str,
                 specs_table: str, asset_events_table: str, pipelines_table: str, task_events_table: str,
                 run_task_arn: str, clock: Callable[[], datetime] = _utc_now,
                 monotonic: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.client = client
        self.sfn = sfn
        self.resource = resource
        self.tokens_table = tokens_table
        self.task_events_table = task_events_table
        self.subscriptions_table = subscriptions_table
        self.specs_table = specs_table
        self.asset_events_table = asset_events_table
        self.pipelines_table = pipelines_table
        self.run_task_arn = run_task_arn
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self._names: Dict[str, Dict[str, str]] = {}
        self._texts: Dict[str, task_spec.TaskSpec] = {}
        self._texts_bytes: Dict[str, int] = {}

    # ── entry points ─────────────────────────────────────────────────────

    def full_pass(self, run_key: str, *, source: str, remaining_ms: Callable[[], int], check: bool = False,
                  orphan_keys: Sequence[str] = ()) -> Dict[str, Any]:
        """A pass over every row of the run, which also decides the run's end (§4).

        ``check`` makes it the sweeper's check of the run: it judges executions and
        reports in ``lost`` the rows whose wait ended on a lost hint (§7).
        ``orphan_keys`` are rows to stop if the run row is missing (invariant 11).
        """
        return self._pass(run_key, None, source=source, remaining_ms=remaining_ms, orphan_keys=set(orphan_keys),
                          check=check)

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
              orphan_keys: Optional[Set[str]] = None, check: bool = False) -> Dict[str, Any]:
        deadline = self.monotonic() + max(0, remaining_ms() - RESERVE_MS) / 1000
        try:
            return self._run_pass(run_key, targets, source=source, deadline=deadline,
                                  orphan_keys=orphan_keys or set(targets or ()), check=check)
        except CoordinationTransient:
            raise
        except (ClientError, BotoCoreError) as exc:
            failures.reraise_transient(exc)
            return self._failed(run_key, source, exc)
        except Exception as exc:  # noqa: BLE001 — one bad run never holds up its shard (Rules 51, 52)
            return self._failed(run_key, source, exc)

    def _run_pass(self, run_key: str, targets: Optional[Set[str]], *, source: str, deadline: float,
                  orphan_keys: Set[str], check: bool) -> Dict[str, Any]:
        now = self.clock()
        run_item = self._get(run_key)
        if run_item is None:
            # Every row of the run, not only the ones that woke this pass: a row that waits for other rows has no
            # due time, so no check of its own would come (ADR #137 §3, invariant 11).
            keys = orphan_keys | set(task_row.run_rows(self.client, self.tokens_table,
                                                         run_key[len(run_row.KEY_PREFIX):]))
            return self.stop_orphans(run_key, sorted(keys), source=source, deadline=deadline)
        run = dispatch.run_view(run_item)
        if check and run.status == run_row.CREATING:
            run = self._engine_check(run, now, finished=False) or run     # it never became active (§4)
        if not run.active and not run.stopping:
            return {"run": run_key, "source": source, "action": "creating"}    # nothing starts before (§1)
        names = self._spec_names(run_item)
        keys = {name: task_row.key(name, run.date, run.execution) for name in names}
        if targets is None:
            judged = set(names)
        else:
            judged = {name for name, key in keys.items() if key in targets}
        state = _Pass(run, {}, {}, names, deadline, source, now, check=check, left_read=run.left_at is not None)
        self._read_rows(state, {name: keys[name] for name in judged})
        if targets is not None:
            # A narrow pass also reads the upstreams its rows name (D117: the rows hold the dependencies).
            upstreams = {d for name in judged if name in state.rows for d in state.rows[name].dependencies} - judged
            self._read_rows(state, {name: keys[name] for name in upstreams if name in keys})
        try:
            if targets is None and run.requests:
                self._requests(state)                  # the renewal, then a reset: before any decision (§3, §5)
            if run.stopping:
                self._stop_rows(state, judged)
            elif targets is not None and run_row.RESET_DEPENDANTS in run.requests:
                state.left = True       # a reset waits on the run: its full pass decides these rows after it (§3)
            else:
                dependencies = {name: state.rows[name].dependencies for name in judged if name in state.rows}
                for name in dispatch.upstreams_first(judged, dependencies):
                    if name in state.rows:
                        self._decide(state, name)
            if targets is None:
                end = self._run_end(state, keys)
                stopped = self._engine_check(state.run, now, finished=end.finished) if check else None
                if stopped is not None:                    # its execution ended before its answer (§4)
                    state.run = stopped
                    self._stop_rows(state, judged)
                    self._run_end(state, keys)
        except _Left:
            state.left = True
        if state.left:
            self._send(dispatch_writes.due_now(state.run, table=self.tokens_table, now=now,
                                               left=bool(state.changed) or state.run.left_at is None), quiet=True)
        result: Dict[str, Any] = {"run": run_key, "source": source, "rows": len(state.rows), "changed": state.changed,
                                  "started": state.started, "left": state.left}
        if check:
            result["lost"] = sorted(state.lost)
        log.info("dispatcher", "pass", **result)
        return result

    # ── requests: the heavy actions a pass carries out (§3, §5) ────────

    def _requests(self, state: _Pass) -> None:
        """The run's renewal, then the reset of a Restart with dependants unless a stop is in progress; each is
        cleared once carried out, on the run_version the pass holds."""
        renewal = state.run.requests.get(run_row.RENEW_TTL)
        if renewal is not None:
            self._renew(state, renewal)
        reset = state.run.requests.get(run_row.RESET_DEPENDANTS)
        if reset is not None and not state.run.stopping:
            self._reset(state, reset)

    def _renew(self, state: _Pass, request: Mapping[str, Any]) -> None:
        """The renewal of a Restart (§5): the pointers of each waiting row with wait_for, before any decision, so
        no fact recorded after the Restart goes unseen; then the TTLs, one write at a time within the budget. A
        refused condition means the item is gone or renewed already."""
        for name in sorted(state.rows):
            if state.rows[name].status == S.WAITING.value and state.rows[name].has_wait_for:
                self._within_budget(state)
                self._put_pointers(state, name)
        for write in actions.renewals(state.run, state.items, request=request, now=state.now, table=self.tokens_table):
            self._within_budget(state)
            try:
                self.client.update_item(**write)
            except ClientError as exc:
                if not failures.is_condition_failed(exc):
                    raise
        self._clear(state, run_row.RENEW_TTL)

    def _reset(self, state: _Pass, request: Mapping[str, Any]) -> None:
        """The reset of a Restart with dependants (§3): one transaction per row, upstreams first, each raising
        run_version from the pass's read. A refused one leaves the rest to the next pass, which builds its set
        again from new reads; only a pass that resets nothing clears the request."""
        names = actions.reset_set(state.rows, request)
        if not names:
            self._clear(state, run_row.RESET_DEPENDANTS)
            return
        for name in names:
            self._within_budget(state)
            row = state.rows[name]
            if not self._send_items(actions.reset(state.items[name], row, state.run, now=state.now,
                                                  table=self.tokens_table)):
                raise _Left()
            state.run = replace(state.run, version=state.run.version + 1)
            state.items[name] = actions.reset_applied(state.items[name], row, state.run, now=state.now)
            state.rows[name] = dispatch.row_view(state.items[name])
            state.changed += 1
            log.info("dispatcher", "row", run=state.run.key, task=name, status=S.WAITING.value, transition="reset")
            if state.rows[name].has_wait_for:
                self._put_pointers(state, name)

    def _clear(self, state: _Pass, kind: str) -> None:
        """A request cleared on the run_version the pass holds. Refused, an action or a request came after the
        pass's read: the next pass carries the request out again from new reads."""
        if not self._send(actions.clear_request(state.run, kind, table=self.tokens_table, now=state.now)):
            raise _Left()
        requests = {k: v for k, v in state.run.requests.items() if k != kind}
        state.run = replace(state.run, version=state.run.version + 1, requests=requests)

    def _put_pointers(self, state: _Pass, name: str) -> None:
        """A waiting row's asset: pointers, as Start_Run writes them. A lost one is a lost hint: the run row's
        next check judges the row (Rule 41)."""
        for pointer in actions.pointer_items(state.items[name], run_key=state.run.key, now=state.now):
            try:
                self.client.put_item(TableName=self.subscriptions_table,
                                     Item={k: _serializer.serialize(v) for k, v in pointer.items()})
            except (ClientError, BotoCoreError) as exc:
                log.error("dispatcher", "an asset pointer could not be written", run=state.run.key, task=name,
                          pointer=pointer, error=str(exc))
                if failures.error_code(exc) in ACCESS_DENIED:     # a permission no retry brings: say so (§7)
                    self._warn(state.run, subject=f"{state.run.key}-pointers", task=name,
                               item=state.items.get(name, {}),
                               error=f"{name}: its asset pointers cannot be written, so a fact wakes it only at "
                                     f"its run's next check: {failures.error_code(exc)}: {exc}")
                    return

    def _stop_rows(self, state: _Pass, judged: Set[str]) -> None:
        """Every unfinished row of the set stopped, dependants first (§4)."""
        for name in dispatch.stop_order({n: r for n, r in state.rows.items() if n in judged}):
            self._decide(state, name, stop=True)

    def _decide(self, state: _Pass, name: str, *, stop: bool = False) -> None:
        """One row decided and written. A row that cannot be decided fails with the cause if it waits, or warns,
        and the pass goes on to the other rows (§3, Rule 51): one bad row never holds up its run."""
        try:
            self._row(state, name, stop=stop)
        except (_Left, CoordinationTransient):
            raise
        except (ClientError, BotoCoreError) as exc:
            failures.reraise_transient(exc)
            self._undecidable(state, name, exc)
        except Exception as exc:  # noqa: BLE001 — one row that cannot be decided never holds up its run (Rule 51)
            self._undecidable(state, name, exc)

    def _undecidable(self, state: _Pass, name: str, exc: BaseException) -> None:
        cause = f"it could not be decided: {type(exc).__name__}: {exc}"
        log.error("dispatcher", "a row could not be decided", run=state.run.key, task=name, error=str(exc))
        row = state.rows.get(name)
        if row is None:
            return
        if failures.error_code(exc) in ACCESS_DENIED:              # it waits for the permission (§7)
            self._warn(state.run, subject=row.key, task=name, item=state.items.get(name, {}), error=f"{name}: {cause}")
            return
        judgment = dispatch.undecidable(row, now=state.now, cause=cause)
        try:
            written = self._write(state, row, judgment)
        except (ClientError, BotoCoreError) as write_exc:
            failures.reraise_transient(write_exc)
            self._warn(state.run, subject=row.key, task=name, item=state.items.get(name, {}),
                       error=f"{name}: {cause}; its failure could not be written: {write_exc}")
            return
        if written:
            self._after(state, name, row, judgment)
        else:
            state.left = True                                      # it moved meanwhile: the next pass decides it

    def _row(self, state: _Pass, name: str, *, stop: bool = False) -> None:
        """Decide one row and send what was decided; decide it again once from new reads if refused."""
        for attempt in range(2):
            row = state.rows[name]
            judgment = self._judge(state, name, stop=stop)
            items = dispatch_writes.task_write(row, judgment, state.run, table=self.tokens_table, now=state.now)
            if not items and not judgment.start and not judgment.warning:
                return                                         # nothing to send: no time spent
            self._within_budget(state)
            if not items or self._send_items(items):
                self._after(state, name, row, judgment)
                return
            if attempt == 0:
                self._read_again(state, name)
        state.left = True                                          # refused twice: the next pass takes it (§7)

    def _within_budget(self, state: _Pass) -> None:
        if self.monotonic() > state.deadline:
            raise _Left()

    def _judge(self, state: _Pass, name: str, *, stop: bool) -> dispatch.Judgment:
        row = state.rows[name]
        if stop:
            return dispatch.stop(row, now=state.now, cause=_stop_cause(state.run))
        if state.check and dispatch.liveness_due(row, state.run, now=state.now):
            self._within_budget(state)                         # judging an execution takes a call
            execution = self._describe(_execution_arn(self.run_task_arn, row.key, state.run, row.generation),
                                       state.run, subject=row.key)
            if execution is not None:                  # an unknown state judges nothing (invariant 10)
                return dispatch.liveness(row, state.run, now=state.now, execution=execution)
        upstreams = {d: state.rows.get(d) for d in row.dependencies}
        assets = None
        if dispatch.needs_assets(row, upstreams):
            assets = self._assets(state, name)
        return dispatch.judge(row, upstreams, state.run, now=state.now, assets=assets)

    def _after(self, state: _Pass, name: str, row: dispatch.RowView, judgment: dispatch.Judgment) -> None:
        """What follows a write that went in: the row as dependants now read it, its pointers, a warning, a start."""
        if judgment.transition or judgment.fields or judgment.removed or judgment.due != row.due:
            state.changed += 1
            log.info("dispatcher", "row", run=state.run.key, task=name, status=judgment.status or row.status,
                     transition=judgment.transition, cause=judgment.cause)
        # The row as its write left it: its status, generation, times and due time (dispatch_writes.applied).
        state.items[name] = dispatch_writes.applied(state.items[name], row, judgment, state.run, now=state.now)
        state.rows[name] = dispatch.row_view(state.items[name])
        if judgment.leaves_wait and row.has_wait_for:
            self._drop_pointers(state.items[name])
        if state.check and judgment.since is not None:
            countdown = row.times.countdown_end
            if sweep.lost_hint(judgment.since, state.now, left_at=state.left_read,
                               countdown_ended=countdown is not None and countdown <= state.now,
                               resumed_at=pauses.resumed_at(state.run.pauses)):
                state.lost.append(name)
        if judgment.transition in records.DECISION_EVENTS or judgment.transition in records.FAILURE_EVENTS:
            self._timeline(state, name, row, judgment)
        if judgment.warning:
            self._warn(state.run, subject=row.key, task=name, item=state.items[name], error=judgment.warning)
        if judgment.start:
            self._start(state, name, row)
        if judgment.transition == "stop" and row.status in _STARTED:
            self._stop_run_task(state, name, row)

    def _stop_run_task(self, state: _Pass, name: str, row: dispatch.RowView) -> None:
        """The run_task of a row a stop ended, as cleanup (§4): the fence keeps whatever it does next out of the
        row, the output and the facts. Its work may run on if the stop fails, so a failure warns (Principle #38)."""
        arn = _execution_arn(self.run_task_arn, row.key, state.run, row.generation)
        try:
            self.sfn.stop_execution(executionArn=arn, error=STOPPED_ERROR, cause=_stop_cause(state.run))
        except (ClientError, BotoCoreError) as exc:
            if failures.error_code(exc) == "ExecutionDoesNotExist":
                return                                     # its start never went through
            # No pass comes back to a stopped row, so even a transient failure is not raised: it is said.
            self._warn(state.run, subject=f"{row.key}-stop", task=name, item=state.items.get(name, {}),
                       error=f"{name}: its run_task could not be stopped, and its work may run on; its result is "
                             f"not recorded: {failures.error_code(exc)}: {exc}")

    def _timeline(self, state: _Pass, name: str, row: dispatch.RowView, judgment: dispatch.Judgment) -> None:
        """The decision's event in the task's timeline, or the end of a task it failed (ADR #137 §1). Best effort,
        as 1.2's wrapper wrote it: the row holds the state, and only the console's timeline reads the events; a
        failure is logged."""
        common = dict(task_run_id=run_name.run_task_name(row.key, state.run.run_start, row.generation),
                      execution_name=row.key, task_name=name, pipeline_execution=state.run.execution,
                      partition_key=state.run.date,
                      backfill_id=str(state.items[name].get(task_row.BACKFILL_ID) or ""), now=state.now)
        if judgment.transition in records.FAILURE_EVENTS:
            event = records.failure_event(**common, cause=str(judgment.cause or ""))
        else:
            event = records.decision_event(transition=str(judgment.transition), **common,
                                           dependencies=list(row.dependencies),
                                           reasons=[str(r) for r in judgment.fields.get(task_row.REASONS) or []])
        try:
            self.client.put_item(TableName=self.task_events_table,
                                 Item={k: _serializer.serialize(v) for k, v in event.items()})
        except (ClientError, BotoCoreError) as exc:
            log.error("dispatcher", "could not write the decision to the timeline", run=state.run.key, task=name,
                      event=event["event_type"], error=str(exc))

    def _read_again(self, state: _Pass, name: str) -> None:
        """A refused write: the run row, the row and its upstreams read again (§1)."""
        run_item = self._get(state.run.key)
        if run_item is None:
            raise _Left()
        fresh = dispatch.run_view(run_item)
        moved = fresh.version != state.run.version
        state.run = fresh
        if moved:
            # An action, a stop mark or a request came after the pass's read: what it decided from that read no
            # longer holds, so the next pass decides the rest from new reads (§1, §3, §4).
            raise _Left()
        row = state.rows[name]
        keys = {n: task_row.key(n, state.run.date, state.run.execution) for n in (name, *row.dependencies)}
        self._read_rows(state, keys)
        if name not in state.rows:
            raise _Left()

    # ── the run's end ────────────────────────────────────────────────────

    def _run_end(self, state: _Pass, keys: Mapping[str, str]) -> dispatch.RunEnd:
        """The summary, the answer, the stop done and the run row's due time (§3, §4)."""
        statuses = {name: (state.rows[name].status if name in state.rows else None) for name in keys}
        end = dispatch.run_outcome(state.run, statuses)
        run = state.run
        if end.missing and run.active:          # a run that never became active may lack rows: Start_Run warned
            self._warn(run, subject=f"{run.key}-missing", task="", item={},
                       error=f"rows of the run are missing, and count as failed: {', '.join(end.missing)}")
        if dispatch.summary_changed(run, end):
            self._send(dispatch_writes.summary(run, end, table=self.tokens_table, read_at=state.now), quiet=True)
        if dispatch.needs_answer(run, end) and run.stopping and (run.stop or {}).get("by") != run_row.ENGINE:
            self._stop_execution(run)                  # a Stop of the run ends its execution, then the answer (§4)
        if dispatch.needs_answer(run, end) and self._answer(run, end):
            if self._send(dispatch_writes.answered(run, table=self.tokens_table, now=state.now)):
                run = replace(run, version=run.version + 1, answered_order=run.token_order)
        if end.finished and run.answered:
            if run.stopping and self._send(dispatch_writes.stop_done(run, table=self.tokens_table, now=state.now)):
                run = replace(run, version=run.version + 1, stop_done=True)
            if not run.stopping and not run.requests:
                self._send(dispatch_writes.close(run, table=self.tokens_table, now=state.now), quiet=True)
        state.run = run
        return end

    def _stop_execution(self, run: dispatch.RunView) -> None:
        """The run's execution stopped once every row of a stopped run is final (§4); the answer that follows meets
        a closed token, which counts as answered. If it cannot be stopped, the answer ends it as failed."""
        if not run.execution_arn:
            return
        try:
            self.sfn.stop_execution(executionArn=run.execution_arn, error=STOPPED_ERROR, cause=_stop_cause(run))
        except ClientError as exc:
            failures.reraise_transient(exc)
            log.error("dispatcher", "the stopped run's execution could not be stopped; the answer ends it",
                      run=run.key, error=str(exc))

    # ── executions: the sweeper's checks (§1, §4) ────────────────────────

    def _engine_check(self, run: dispatch.RunView, now: datetime, *, finished: bool) -> Optional[dispatch.RunView]:
        """The engine's stop of a run whose execution ended before its answer, or that never became active (§4).

        Returns the run as the stop mark left it, or None when the run goes on or
        the mark's write was refused: another write raised ``run_version``, or a
        Stop of the run came first; the next check decides again.
        """
        if not dispatch.engine_check_due(run, now=now, finished=finished):
            return None
        cause = dispatch.engine_stop_cause(run, now=now, execution=self._describe_run(run), finished=finished)
        if cause is None:
            return None
        if not self._send(dispatch_writes.engine_stop(run, table=self.tokens_table, cause=cause, now=now)):
            return None
        log.info("dispatcher", "the engine stops the run", run=run.key, cause=cause)
        mark = run_row.stop_mark(by=run_row.ENGINE, reason=cause, at=format_utc(now))
        return replace(run, version=run.version + 1, stop=mark)

    def _describe_run(self, run: dispatch.RunView) -> Optional[dispatch.Execution]:
        """The run's execution, by the ARN Start_Run recorded on the run row ($states.context.Execution.Id): the
        engine's check needs no registry entry, which a large pipeline's registration may not have written (the
        review of TR-4). None when unknown, which stops nothing (invariant 10)."""
        if not run.execution_arn:
            log.error("dispatcher", "a run row names no execution ARN; its execution is not checked", run=run.key)
            return None
        return self._describe(run.execution_arn, run, subject=run.key)

    def _describe(self, arn: str, run: dispatch.RunView, *, subject: str) -> Optional[dispatch.Execution]:
        """What DescribeExecution says of ``arn``; None when it could not say, which decides nothing (invariant 10)."""
        try:
            response = self.sfn.describe_execution(executionArn=arn)
        except ClientError as exc:
            code = failures.error_code(exc)
            if code == NO_EXECUTION:
                return dispatch.Execution(None)
            log.error("dispatcher", "an execution could not be described", execution=arn, error=str(exc))
            if not failures.is_transient(exc):
                self._warn(run, subject=f"{subject}-describe", task="", item={},
                           error=f"the execution {arn} could not be checked: {code}: {exc}")
            return None
        except BotoCoreError as exc:
            log.error("dispatcher", "an execution could not be described", execution=arn, error=str(exc))
            return None
        return dispatch.Execution(str(response.get("status") or ""), response.get("error"), response.get("cause"))

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
            done = dispatch.started(started, state.run, now=state.now)
            if self._write(state, started, done):      # earlier attempts failed: liveness counts from now
                self._after(state, name, started, done)
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
        missing = sorted(d for d in row.dependencies if d not in state.rows)
        if missing:
            return dispatch.start_failed(row, run, now=state.now, error=f"the row of its upstream {missing[0]} is missing",
                                         kind=dispatch.REFUSED)
        upstreams = {d: (state.rows[d].status, state.rows[d].skip_origin) for d in row.dependencies}
        try:
            text = run_task_input.build(
                execution_name=row.key, generation=row.generation, task_name=name,
                pipeline_name=str(item.get(task_row.PIPELINE, "")), pipeline_execution=run.execution,
                date=run.date, partition_arg=state.inputs.partition_arg,
                backfill_id=item.get(task_row.BACKFILL_ID), spec=self._spec_text(item, name),
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

    def stop_orphans(self, run_key: str, keys: Sequence[str], *, source: str,
                     deadline: Optional[float] = None) -> Dict[str, Any]:
        """Each row named, stopped with the cause: its run row is missing (invariant 11). A row that a 1.2 run
        left in flight has no readiness fields; its stop needs only its status and generation (*Migration*).
        Past ``deadline`` the rest wait for a later pass or check, which finds them again."""
        now = self.clock()
        stopped = 0
        for index, key in enumerate(keys):
            if deadline is not None and self.monotonic() >= deadline:
                log.error("dispatcher", "rows whose run row is missing are left for a later pass", run=run_key,
                          left=len(keys) - index)
                break
            item = self._get(key)
            if item is None or task_row.STATUS not in item:
                continue
            row = dispatch.stop_view(item)
            judgment = dispatch.stop(row, now=now, cause="its run row is missing")
            items = dispatch_writes.task_write(row, judgment, None, table=self.tokens_table, now=now)
            if items and self._send_items(items):
                stopped += 1
        result = {"run": run_key, "source": source, "action": "orphans", "stopped": stopped}
        log.info("dispatcher", "pass", **result)
        return result

    # ── reads ────────────────────────────────────────────────────────────

    def _spec_names(self, run_item: Mapping[str, Any]) -> Mapping[str, str]:
        """The run's task ids and their texts' hashes, read once per spec version and kept (D117)."""
        spec_id = str(run_item[run_row.SPEC_ID])
        known = self._names.get(spec_id)
        if known is None:
            known = spec_store.read_names(self.client, self.specs_table, spec_id,
                                          expected_count=int(run_item[run_row.TASK_COUNT]))
            while self._names and sum(map(len, self._names.values())) + len(known) > NAMES_CACHE:
                self._names.pop(next(iter(self._names)))           # the oldest version goes first
            self._names[spec_id] = known
        return known

    def _spec_text(self, item: Mapping[str, Any], name: str) -> task_spec.TaskSpec:
        """A task's spec, read by the hash its row carries with one GetItem, and kept within TEXTS_CACHE_BYTES:
        a pass loads no whole version, whatever the pipeline's size (D117)."""
        digest = str(item[task_row.SPEC_HASH])
        known = self._texts.get(digest)
        if known is None:
            known = spec_store.read_text(self.client, self.specs_table, str(item[task_row.PIPELINE]), digest)
            size = len(task_spec.canonical_json(dict(known.fields)).encode("utf-8"))
            while self._texts and sum(self._texts_bytes.values()) + size > TEXTS_CACHE_BYTES:
                oldest = next(iter(self._texts))
                self._texts.pop(oldest)
                self._texts_bytes.pop(oldest)
            self._texts[digest], self._texts_bytes[digest] = known, size
        if known.task_id != name:
            raise task_spec.SpecError(f"the row of {name} names the spec of {known.task_id}")
        return known

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
            view = None
            if read is not None and task_row.of_run(read, state.run.execution, state.run.run_start):
                try:
                    view = dispatch.row_view(read)
                except (ValueError, KeyError, TypeError) as exc:
                    # Rule 51: a row no reader can parse counts as missing, and the bell says so.
                    self._warn(state.run, subject=key, task=name, item=read,
                               error=f"{name}: its row cannot be read, so it counts as missing: {exc}")
            if view is not None:
                state.items[name] = read
                state.rows[name] = view
            else:
                state.items.pop(name, None)
                state.rows.pop(name, None)

    def _assets(self, state: _Pass, name: str) -> Evaluation:
        """The evaluation of the row's facts, read through ``stores.FactStore`` (ADR #130 §4)."""
        wait_for = task_spec.readiness_from_text(str(state.items[name][task_row.READINESS])).get(
            TaskSpecKey.WAIT_FOR.value)
        text = json.dumps(wait_for, sort_keys=True)
        known = state.facts.get(text)
        if known is not None:
            return known                                       # the rows that wait for the same facts read them once
        self._within_budget(state)                             # reading the facts takes time
        registry = Registry(self.resource, self.pipelines_table)
        reader = FactStore(self.resource, self.asset_events_table, registry, state.now,
                           on_unreadable=lambda item, exc: self._unreadable_fact(state.run, item, exc))
        evaluation = evaluate_assets(asset_spec.parse(wait_for), target=partition_date(state.run.date), now=state.now,
                                     reader=reader)
        state.facts[text] = evaluation
        return evaluation

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
                if failures.error_code(exc) == "TransactionConflictException":
                    # A single write that met a transaction on its row (run_task's success at the same moment): the
                    # same short backoff as a cancelled transaction, then decided again from new reads (§7). It
                    # ended the whole pass as transient (the review of TR-5).
                    if attempt + 1 < CONFLICT_ATTEMPTS:
                        self.sleep(random.uniform(*CONFLICT_BACKOFF_SECONDS))
                        continue
                    return False
                if failures.error_code(exc) == "TransactionCanceledException":
                    reasons = [str(r.get("Code", "")) for r in exc.response.get("CancellationReasons", [])]
                    if "ThrottlingError" in reasons:
                        raise CoordinationTransient(f"a transaction was throttled: {exc}") from exc
                    if CONFLICT in reasons and attempt + 1 < CONFLICT_ATTEMPTS:
                        self.sleep(random.uniform(*CONFLICT_BACKOFF_SECONDS))
                        continue
                    # A condition, or other writes met again and again: decided again from new reads, or left to
                    # the next pass (§7). Neither is a failure.
                    return False
                failures.reraise_transient(exc)
                raise
        return False

    def _send(self, update: Dict[str, Any], *, quiet: bool = False) -> bool:
        """A write of the run row; a refused condition, or a transaction on the row at the same moment, is no
        error: another pass or an action came first, and the next pass or check writes it again."""
        try:
            self.client.update_item(**update)
            return True
        except ClientError as exc:
            if failures.is_condition_failed(exc) or failures.error_code(exc) == "TransactionConflictException":
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


def _arn_of(state_machine: str, execution: str) -> str:
    """An execution's ARN from its state machine's (CLAUDE.md, "Stop pipeline hierarchy")."""
    return f"{state_machine.replace(':stateMachine:', ':execution:')}:{execution}"


def _execution_arn(run_task_arn: str, row_key: str, run: dispatch.RunView, generation: int) -> str:
    """The ARN of a row's run_task execution of ``generation``, under its derived name (invariant 4)."""
    return _arn_of(run_task_arn, run_name.run_task_name(row_key, run.run_start, generation))


def _stop_cause(run: dispatch.RunView) -> str:
    stop = run.stop or {}
    reason = str(stop.get("reason") or "no reason given")
    return f"the run was stopped by {stop.get('by') or 'the engine'}: {reason}"


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
                      pipelines_table=env.PIPELINES_TABLE, task_events_table=env.TASK_EVENTS_TABLE,
                      run_task_arn=env.RUN_TASK_ARN)


def _dispatcher() -> Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        _DISPATCHER = from_env()
    return _DISPATCHER


def handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    """The tasks table's stream (§7)."""
    remaining: Callable[[], int] = context.get_remaining_time_in_millis if context else (lambda: 10 ** 9)
    return _dispatcher().handle(list(event.get("Records", [])), remaining)
