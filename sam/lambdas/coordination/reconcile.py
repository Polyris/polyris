"""The reconciler: decide from facts, then signal (ADR #130 §3).

The only code that decides whether a waiting task is ready, and the only code
that sends its dependency signal. Callers (registration, notify_dependents,
AssetFanout, resume, the sweeper) only say "check this".

Payloads:
    {"execution_name": "...", "source": "...", "known_events": [...]}
    {"pipeline_execution": "...", "source": "resume"}

Contract: :meth:`Reconciler.reconcile` raises only ``CoordinationTransient``.
Every other failure is handled where it happens (ADR #130 §9):
    * a decision that cannot be made fails the task with the real cause
      (``SendTaskFailure`` → the wrapper's failure path);
    * anything that cannot fail a task becomes a warning row in the console.
So a caller that retries — a stream, an SFN ``Retry``, the sweeper — retries
exactly the failures a retry can fix, and one bad row never blocks a stream.
"""

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import BotoCoreError, ClientError

import env
from coordination_core import decide as dc
from coordination_core import ownership
from coordination_core import spec as sp
from coordination_core import sweep
from coordination_core import trigger_rules as tr
from coordination_core import wake
from coordination_core.asset_conditions import evaluate as evaluate_assets
from coordination_core.partition import partition_date
from coordination_core.records import warning_item
from coordination_core.times import format_utc, parse_utc
from coordination_core.statuses import RECONCILABLE, SKIPPED, WAITING_PAUSED
from failures import CoordinationTransient, reraise_transient
from logger import log
from signals import ACCEPTED, Signals
from stores import FactStore, PauseFlag, Registry, SubscriptionStore, TaskStore

#: What the wrapper's Check_Deps_Signal state routes on.
SIGNALS = {dc.READY: wake.DEPS_READY, dc.SKIP: wake.DEPS_SKIP, dc.UPSTREAM_FAILED: wake.DEPS_BLOCKED}
#: Verdict reported when no decision could be made.
ERROR = "error"
#: Resume handles a run's rows for this long, then leaves the rest due for the sweeper. The console's
#: resume call must answer within the HTTP API's 30 s: the pause releases before this call, a cold
#: start and the deferral writes after the budget share the rest.
RESUME_BUDGET_SECONDS = 10.0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_list(value: Any, field: str) -> List[Any]:
    """A list attribute the registration SFN stores as a JSON string."""
    if value in (None, ""):
        return []
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, list):
        raise ValueError(f"task attribute {field!r} must be a JSON list, got {type(parsed).__name__}")
    return parsed


def _aws(call: Callable[[], Any]) -> Any:
    """Run one AWS call; a transient failure becomes ``CoordinationTransient``."""
    try:
        return call()
    except (ClientError, BotoCoreError) as exc:
        reraise_transient(exc)
        raise


class Reconciler:
    def __init__(
        self,
        resource: Any,
        sfn_client: Any,
        *,
        tokens_table: str = env.TOKENS_TABLE,
        subscriptions_table: str = env.SUBSCRIPTIONS_TABLE,
        asset_events_table: str = env.ASSET_EVENTS_TABLE,
        pipelines_table: str = env.PIPELINES_TABLE,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        budget_seconds: float = RESUME_BUDGET_SECONDS,
    ):
        self.resource = resource
        self.tasks = TaskStore(resource, tokens_table)
        self.subscriptions = SubscriptionStore(resource, subscriptions_table)
        self.signals = Signals(sfn_client)
        self.asset_events_table = asset_events_table
        self.pipelines_table = pipelines_table
        self.clock = clock
        self.monotonic = monotonic
        self.budget_seconds = budget_seconds

    # ── entry points ────────────────────────────────────────────────────

    def reconcile(self, execution_name: str, *, source: str,
                  known_events: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
        """Check one task row. Raises only ``CoordinationTransient``."""
        started = self.monotonic()
        row: Dict[str, Any] = {"execution_name": execution_name}
        try:
            fetched = _aws(lambda: self.tasks.get(execution_name))
            if fetched is None:
                result = self._result(row, source, dc.NOOP, "none", ("task row not found",))
            else:
                row = fetched
                result = self._reconcile_row(row, source, known_events)
        except CoordinationTransient:
            raise
        except Exception as exc:  # noqa: BLE001 — the contract above: only transient failures escape
            reraise_transient(exc)
            result = self._unexpected(row, source, exc)
        result["latency_ms"] = int((self.monotonic() - started) * 1000)
        log.info("reconcile", f"{result['verdict']}/{result['action']}", **result)
        return result

    def reconcile_run(self, pipeline_execution: str, *, source: str) -> Dict[str, Any]:
        """Check every waiting row of one run, within the time budget.

        Rows not handled in time, or hit by a transient failure, are made due
        from the run's resume time, so the next sweeper run handles them and
        does not count them as primary-path misses (ADR #130 §6.5).
        """
        deadline = self.monotonic() + self.budget_seconds
        rows = _aws(lambda: self.tasks.run_rows(pipeline_execution))
        due = _aws(lambda: self.tasks.pause_flag(pipeline_execution)).resumed_at or self.clock()
        handled = deferred = 0
        for row in rows:
            name = str(row["execution_name"])
            if self.monotonic() >= deadline:
                self._defer(row, due)
                deferred += 1
                continue
            try:
                self.reconcile(name, source=source)
                handled += 1
            except CoordinationTransient as exc:
                log.error("reconcile_run", "transient failure; the sweeper retries", execution_name=name, error=str(exc))
                self._defer(row, due)
                deferred += 1
        result = {"pipeline_execution": pipeline_execution, "source": source, "handled": handled, "deferred": deferred}
        log.info("reconcile_run", "done", **result)
        return result

    # ── decision ────────────────────────────────────────────────────────

    def _reconcile_row(self, row: Dict[str, Any], source: str,
                       known_events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        status = str(row.get("status", ""))
        if status not in RECONCILABLE or row.get("pause_token"):
            return self._noop(row, status, source)
        token = row.get("wait_token")
        if not token:
            reason = "the waiting row has no wait_token, so its wrapper cannot be signalled"
            self._warn(row, "coordination", reason)
            return self._result(row, source, ERROR, "warned", (reason,))
        try:
            decision, enabled_at = self._decide(row, status, known_events)
        except CoordinationTransient:
            raise
        except Exception as exc:  # noqa: BLE001 — ADR #130 §3.5: a decision that cannot be made fails the task with its real cause
            reraise_transient(exc)
            return self._fail_task(row, str(token), exc, source)
        result = self._act(row, str(token), decision, source)
        if result["action"] == "signalled" and enabled_at is not None:
            result["enabled_at"] = format_utc(enabled_at)
        return result

    def _decide(self, row: Dict[str, Any], status: str,
                known_events: Sequence[Dict[str, Any]]) -> Tuple[dc.Decision, Optional[datetime]]:
        """The decision, and its enabling time (ADR #130 §9): when the task could first run.

        That is the latest of when its dependencies allowed it, when its
        assets did, its ``started_at`` and its run's last resume.
        """
        dependencies = tuple(str(d) for d in _json_list(row.get("dependencies"), "dependencies"))
        spec = sp.parse(_json_list(row.get("wait_for"), "wait_for"))
        view = dc.TaskView(
            status=status,
            has_pause_token=bool(row.get("pause_token")),
            trigger_rule=str(row.get("trigger_rule") or "all_success"),
            dependencies=dependencies,
            has_asset_conditions=bool(spec.members),
        )
        date_key = str(row["date"])

        statuses: List[str] = []
        rule_skips: List[bool] = []
        started = parse_utc(str(row["started_at"])) if row.get("started_at") else None
        dependencies_enabled: Optional[datetime] = None
        if dependencies:
            short = str(row["pipeline_execution_short"])
            names = [f"{dep}-{date_key}-{short}" for dep in dependencies]
            facts = _aws(lambda: self.tasks.dependency_facts(names))
            statuses = [facts[n].status for n in names]
            rule_skips = [facts[n].status == SKIPPED and facts[n].skip_origin == "rule" for n in names]
            finished = [parse_utc(facts[n].finished_at) if facts[n].finished_at else None for n in names]
            dependencies_enabled = tr.enabled_at(view.trigger_rule, statuses, finished, rule_skips)
            if tr.evaluate(view.trigger_rule, statuses, rule_skips).verdict != tr.READY:
                # Pending or never satisfiable: no asset read can change the outcome. A pause
                # never holds back a skip, so its resume time does not date one.
                return (dc.decide(view, dep_statuses=statuses, rule_originated_skip=rule_skips),
                        _latest([started, dependencies_enabled]))

        assets = None
        if spec.members:
            now = self.clock()
            registry = Registry(self.resource, self.pipelines_table)
            reader = FactStore(self.resource, self.asset_events_table, registry, now, on_unreadable=self.unreadable_fact)
            known = [f for f in map(reader.read, known_events) if f is not None]
            assets = evaluate_assets(spec, target=partition_date(date_key), now=now, reader=reader, known_facts=known)

        pipeline_execution = str(row.get("pipeline_execution") or "")
        flag = _aws(lambda: self.tasks.pause_flag(pipeline_execution)) if pipeline_execution else PauseFlag(False, None)
        decision = dc.decide(view, dep_statuses=statuses, rule_originated_skip=rule_skips, assets=assets,
                             paused=flag.paused)
        return decision, _latest([started, flag.resumed_at, dependencies_enabled,
                                  assets.enabled_at if assets is not None else None])

    # ── actions ─────────────────────────────────────────────────────────

    def _act(self, row: Dict[str, Any], token: str, decision: dc.Decision, source: str) -> Dict[str, Any]:
        name = str(row["execution_name"])
        if decision.verdict in (dc.WAIT, dc.PAUSED) and source == sweep.SOURCE and self._past_liveness(row):
            # ADR #130 §3 step 3: a zombie is found without a signal too, once no wrapper retry can be left.
            return self._check_wrapper(row, token, decision, source)
        if decision.verdict == dc.WAIT:
            return self._decided(row, source, decision, "none")
        if decision.verdict == dc.PAUSED:
            if row.get("status") == WAITING_PAUSED:
                return self._decided(row, source, decision, "none")
            started_at = str(row.get("started_at"))
            parked = _aws(lambda: self.tasks.park(name, token, started_at))
            return self._decided(row, source, decision, "parked" if parked else "superseded")

        payload = {"signal": SIGNALS[decision.verdict], "reason": "; ".join(decision.reasons)}
        try:
            outcome = self.signals.succeed(token, payload)
        except CoordinationTransient:
            raise
        except (ClientError, BotoCoreError) as exc:
            return self._fail_task(row, token, exc, source)
        if outcome == ACCEPTED:
            self._delete_pointers(row)
            return self._decided(row, source, decision, "signalled")
        return self._dead_token(row, token, decision, source)

    def _dead_token(self, row: Dict[str, Any], token: str, decision: dc.Decision, source: str) -> Dict[str, Any]:
        """ADR #130 §3 step 3: the token can no longer be signalled."""
        name = str(row["execution_name"])
        arn = str(row.get("wrapper_execution_arn") or "")
        if not arn:
            reason = "the task token is dead and the row names no wrapper execution to check"
            self._warn(row, "zombie", reason)
            return self._result(row, source, decision.verdict, "warned", (reason,), decision)
        wrapper = _aws(lambda: self.signals.execution_status(arn))
        if wrapper == "RUNNING":
            # A concurrent wake consumed the token; the wrapper owns the status now.
            return self._decided(row, source, decision, "consumed")
        started_at = str(row.get("started_at"))
        now = self.clock()
        if wrapper == "SUCCEEDED" and decision.verdict == dc.SKIP:
            # A pre-upgrade wrapper consumed the skip and expected the sender to write the status.
            reason = "; ".join(decision.reasons)
            closed = _aws(lambda: self.tasks.close_skipped(name, token, started_at, reason, now=now))
            return self._decided(row, source, decision, "skipped" if closed else "superseded")
        return self._abort_zombie(row, token, decision, source, wrapper)

    def _past_liveness(self, row: Dict[str, Any]) -> bool:
        if not row.get("started_at"):
            return False
        timeout = row.get("orchestration_timeout")
        deadline = sweep.liveness_deadline(parse_utc(str(row["started_at"])), float(timeout) if timeout else None,
                                           legacy=ownership.is_legacy(row.get(ownership.FIELD)))
        return self.clock() >= deadline

    def _check_wrapper(self, row: Dict[str, Any], token: str, decision: dc.Decision, source: str) -> Dict[str, Any]:
        """A waiting row past its liveness deadline: is its wrapper still there to be woken?"""
        arn = str(row.get("wrapper_execution_arn") or "")
        wrapper = _aws(lambda: self.signals.execution_status(arn)) if arn else None
        if wrapper == "RUNNING":
            return self._decided(row, source, decision, "alive")
        return self._abort_zombie(row, token, decision, source, wrapper)

    def _abort_zombie(self, row: Dict[str, Any], token: str, decision: Optional[dc.Decision], source: str,
                      wrapper: Optional[str], *, cause: str = "") -> Dict[str, Any]:
        """Close a row whose wrapper is gone; `decision` is None when the decision itself failed with `cause`."""
        name, started_at, now = str(row["execution_name"]), str(row.get("started_at")), self.clock()
        if wrapper is None and row.get("started_at") and now - parse_utc(started_at) < sweep.EXECUTION_VISIBLE_AFTER:
            # Not visible yet is not gone: the row stays, and the sweeper checks it again.
            reason = "the wrapper execution is not visible yet"
            if cause:
                self._warn(row, "coordination", f"{cause}. Failing the task found its token dead, and {reason}.")
            verdict = decision.verdict if decision is not None else ERROR
            return self._result(row, source, verdict, "unconfirmed", (reason,), decision)
        reason = f"the wrapper execution is {wrapper or 'gone'}, so the task can no longer be woken"
        if cause:
            reason = f"{cause}. Failing the task found its token dead, and {reason}"
        closed = _aws(lambda: self.tasks.close_aborted(name, token, started_at, reason, now=now))
        if closed:
            self._warn(row, "zombie", reason)
        verdict = decision.verdict if decision is not None else ERROR
        return self._result(row, source, verdict, "aborted" if closed else "superseded", (reason,), decision)

    def _fail_task(self, row: Dict[str, Any], token: str, exc: BaseException, source: str) -> Dict[str, Any]:
        """ADR #130 §3 step 5: fail the task with the real cause."""
        cause = f"{type(exc).__name__}: {exc}"
        log.error("reconcile", "coordination failed; failing the task", execution_name=row["execution_name"], error=cause)
        try:
            outcome = self.signals.fail(token, cause)
        except CoordinationTransient:
            raise
        except (ClientError, BotoCoreError) as fail_exc:
            self._warn(row, "coordination", f"{cause}. Failing the task also failed: {fail_exc}")
            return self._result(row, source, ERROR, "warned", (cause,))
        if outcome != ACCEPTED:
            arn = str(row.get("wrapper_execution_arn") or "")
            if not arn:
                self._warn(row, "coordination", f"{cause}. The task token is already dead.")
                return self._result(row, source, ERROR, "warned", (cause,))
            wrapper = _aws(lambda: self.signals.execution_status(arn))
            if wrapper == "RUNNING":
                # A concurrent wake consumed the token; the wrapper owns the status now.
                return self._result(row, source, ERROR, "consumed", (cause,))
            # No wrapper is left to fail: the row is a zombie, closed like one (ADR #130 §3 step 3).
            return self._abort_zombie(row, token, None, source, wrapper, cause=cause)
        return self._result(row, source, ERROR, "failed_task", (cause,))

    def _unexpected(self, row: Dict[str, Any], source: str, exc: BaseException) -> Dict[str, Any]:
        cause = f"{type(exc).__name__}: {exc}"
        log.error("reconcile", "unexpected coordination failure", execution_name=row["execution_name"], error=cause)
        self._warn(row, "coordination", cause)
        return self._result(row, source, ERROR, "warned", (cause,))

    def _noop(self, row: Dict[str, Any], status: str, source: str) -> Dict[str, Any]:
        name = str(row["execution_name"])
        view = dc.TaskView(status=status, has_pause_token=bool(row.get("pause_token")), trigger_rule="",
                           dependencies=(), has_asset_conditions=False)
        reasons = dc.decide(view).reasons
        action = "none"
        if row.get("attention") and status not in RECONCILABLE:
            started_at = row.get("started_at")
            cleared = _aws(lambda: self.tasks.clear_attention(
                name, status, None if started_at is None else str(started_at)))
            action = "attention_cleared" if cleared else "superseded"
        return self._result(row, source, dc.NOOP, action, reasons)

    def _delete_pointers(self, row: Dict[str, Any]) -> None:
        """Principle #40: pointers go only after the signal was accepted."""
        name = str(row["execution_name"])
        short = str(row.get("pipeline_execution_short", ""))
        keys = [f"{dep}-{short}" for dep in _json_list(row.get("dependencies"), "dependencies")]
        keys += [f"asset:{a}" for a in sorted(sp.asset_names(sp.parse(_json_list(row.get("wait_for"), "wait_for"))))]
        for key in keys:
            try:
                self.subscriptions.delete(key, name)
            except (ClientError, BotoCoreError) as exc:
                # Harmless: a leftover pointer resolves to a noop and expires by TTL.
                log.warn("reconcile", "pointer delete failed", execution_name=name, dependency_key=key, error=str(exc))

    def _defer(self, row: Dict[str, Any], due: datetime) -> None:
        try:
            self.tasks.mark_due(str(row["execution_name"]), str(row.get("started_at")), due)
        except (ClientError, BotoCoreError) as exc:
            # The sweeper still finds the row at its existing due time.
            log.error("reconcile_run", "could not make the row due now",
                     execution_name=row["execution_name"], error=str(exc))

    def unreadable_fact(self, item: Dict[str, Any], exc: Exception) -> None:
        """A fact no reader can use: logged, and one warning per fact (repeated reads rewrite it)."""
        asset, recorded = str(item.get("asset_name", "")), str(item.get("event_time", ""))
        error = f"The fact of asset {asset} recorded at {recorded} cannot be read, so it counts for no task: {exc}"
        log.error("reconcile", "unreadable asset fact", asset_name=asset, event_time=recorded, error=str(exc))
        try:
            self.tasks.put(warning_item(
                kind="coordination", execution_name=f"fact-{asset}-{recorded}", task_name="",
                pipeline_name=str(item.get("source_dag", "")), pipeline_execution="",
                task_date=str(item.get("partition_key", "")), error=error, now=self.clock(),
            ))
        except (ClientError, BotoCoreError) as write_error:
            log.error("reconcile", "could not write the warning row", asset_name=asset, error=str(write_error))

    def _warn(self, row: Dict[str, Any], kind: str, error: str) -> None:
        item = warning_item(
            kind=kind,
            execution_name=str(row["execution_name"]),
            task_name=str(row.get("task_name", "")),
            pipeline_name=str(row.get("pipeline_name", "")),
            pipeline_execution=str(row.get("pipeline_execution", "")),
            task_date=str(row.get("date", "")),
            error=error,
            now=self.clock(),
        )
        try:
            self.tasks.put(item)
        except (ClientError, BotoCoreError) as exc:
            # The log line is the last trace; the engine-health check reports repeated failures.
            log.error("reconcile", "could not write the warning row", warning=item["execution_name"], error=str(exc))

    # ── results ─────────────────────────────────────────────────────────

    def _decided(self, row: Dict[str, Any], source: str, decision: dc.Decision, action: str) -> Dict[str, Any]:
        return self._result(row, source, decision.verdict, action, decision.reasons, decision)

    @staticmethod
    def _result(row: Dict[str, Any], source: str, verdict: str, action: str, reasons: Tuple[str, ...],
                decision: Optional[dc.Decision] = None) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "execution_name": row["execution_name"], "source": source,
            "verdict": verdict, "action": action, "reasons": list(reasons),
        }
        if decision is not None and decision.deps is not None:
            result["deps"] = {"rule": decision.deps.effective_rule, "verdict": decision.deps.verdict,
                              "counts": asdict(decision.deps.counts)}
        if decision is not None and decision.assets is not None:
            result["assets"] = [{"asset": a.asset_name, "ready": a.ready, "reason": a.reason}
                                for a in decision.assets.assets]
        return result


def _latest(moments: Sequence[Optional[datetime]]) -> Optional[datetime]:
    return max((m for m in moments if m is not None), default=None)


_RECONCILER: Optional[Reconciler] = None


def _reconciler() -> Reconciler:
    global _RECONCILER
    if _RECONCILER is None:
        _RECONCILER = Reconciler(env.dynamodb(), env.stepfunctions())
    return _RECONCILER


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if "pipeline_execution" in event:
        return _reconciler().reconcile_run(str(event["pipeline_execution"]), source=str(event.get("source", "resume")))
    if "execution_name" in event:
        return _reconciler().reconcile(str(event["execution_name"]), source=str(event.get("source", "unknown")),
                                       known_events=event.get("known_events") or ())
    raise ValueError(f"payload needs 'execution_name' or 'pipeline_execution', got keys {sorted(event)}")
