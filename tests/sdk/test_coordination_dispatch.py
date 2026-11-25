"""What a pass of the dispatcher decides for each row and for its run, clause by clause (ADR #137 §1, §4, §5)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from polyris.constants import SkipOrigin, TaskStatus
from polyris.coordination import attention, deadlines, dispatch, run_row, start_run, sweep, task_row, task_spec
from polyris.coordination import transitions
from polyris.coordination.asset_conditions import AssetResult, Evaluation
from polyris.coordination.deadlines import RowTimes
from polyris.coordination.pauses import Interval
from polyris.coordination.times import format_utc

S = TaskStatus
NOW = datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc)
WAITING_SINCE = NOW - timedelta(hours=1)
RUN = dispatch.RunView(key="_run_orders", execution="orders", status=run_row.ACTIVE, version=3, task_count=3)
PAUSED = dispatch.RunView(key="_run_orders", execution="orders", status=run_row.ACTIVE, version=4, task_count=3,
                          pauses=(Interval(NOW - timedelta(minutes=30)),))


def row(task_id: str = "load", *, status: str = S.WAITING.value, deps=(), rule: str = "all_success",
        wait_for: bool = False, wait_before: int = 0, reasons=(), error: Optional[str] = None,
        **times: Any) -> dispatch.RowView:
    times.setdefault("waiting_since", WAITING_SINCE)
    return dispatch.RowView(task_id=task_id, key=f"{task_id}-2026-11-19-run", status=status, generation=1,
                            trigger_rule=rule, dependencies=tuple(deps),
                            times=RowTimes(status=status, has_wait_for=wait_for, **times),
                            wait_before=timedelta(seconds=wait_before),
                            reasons=tuple(reasons), error=error)


def up(task_id: str, status: str, *, finished: datetime = NOW - timedelta(minutes=20),
       origin: Optional[str] = None) -> dispatch.RowView:
    view = row(task_id, status=status)
    return dispatch.RowView(**{**view.__dict__, "finished_at": finished, "skip_origin": origin})


def facts(ready: bool, *, enabled_at: Optional[datetime] = None) -> Evaluation:
    result = AssetResult("lake/prices", "latest", ready, "fresh" if ready else "no fact for 2026-11-19")
    return Evaluation(ready, (result,), enabled_at=enabled_at)


def assert_dispatcher_may(source: str, judgment: dispatch.Judgment) -> None:
    """Every move a pass makes is one the table gives the dispatcher (invariant 7)."""
    if judgment.transition:
        assert transitions.allowed(source, judgment.status, transitions.Writer.DISPATCHER), judgment.transition


# ── a waiting row: its upstreams ────────────────────────────────────────────

def test_a_root_task_is_written_deps_ready_and_started_with_its_first_check_due():
    judgment = dispatch.judge(row(enabled_at=WAITING_SINCE), {}, RUN, now=NOW)
    assert (judgment.transition, judgment.start) == ("ready", True)
    assert judgment.due == NOW + attention.BACKOFF[0]
    assert judgment.fields == {task_row.STATUS_SINCE: format_utc(NOW)}
    assert_dispatcher_may(S.WAITING.value, judgment)


def test_a_row_that_waits_for_its_upstreams_records_why_and_has_no_time_of_its_own():
    judgment = dispatch.judge(row(deps=["extract"]), {"extract": row("extract")}, RUN, now=NOW)
    assert (judgment.transition, judgment.start, judgment.due) == (None, False, None)
    assert judgment.fields == {task_row.REASONS: ["waiting for its upstreams"]}


def test_an_unchanged_wait_writes_nothing():
    waiting = row(deps=["extract"], reasons=["waiting for its upstreams"])
    judgment = dispatch.judge(waiting, {"extract": row("extract")}, RUN, now=NOW)
    assert (judgment.fields, judgment.removed, judgment.due) == ({}, (), None)


def test_its_upstreams_success_starts_it_and_records_when_they_allowed_it():
    judgment = dispatch.judge(row(deps=["extract"]), {"extract": up("extract", S.SUCCESS.value)}, RUN, now=NOW)
    assert (judgment.transition, judgment.start) == ("ready", True)
    assert judgment.fields[task_row.ENABLED_AT] == format_utc(NOW - timedelta(minutes=20))


def test_the_enabling_time_is_never_before_the_wait_began():
    """A Restart begins the wait anew: an old upstream time does not shorten it (deadlines.enabled_since)."""
    judgment = dispatch.judge(row(deps=["extract"], wait_for=True),
                              {"extract": up("extract", S.SUCCESS.value, finished=WAITING_SINCE - timedelta(days=3))},
                              RUN, now=NOW, assets=facts(False))
    assert judgment.fields[task_row.ENABLED_AT] == format_utc(WAITING_SINCE)


@pytest.mark.parametrize("rule,upstream,origin,transition,skip_origin", [
    ("all_success", S.FAILED.value, None, "block", None),
    ("all_success", S.SKIPPED.value, SkipOrigin.RULE, "skip_by_rule", SkipOrigin.RULE),
    ("all_failed", S.SUCCESS.value, None, "skip_by_rule", SkipOrigin.RULE),
])
def test_an_outcome_its_rule_cannot_run_after_ends_the_row_with_the_reason(rule, upstream, origin, transition,
                                                                          skip_origin):
    judgment = dispatch.judge(row(deps=["extract"], rule=rule, reasons=["waiting for its upstreams"]),
                              {"extract": up("extract", upstream, origin=origin)}, RUN, now=NOW)
    assert judgment.transition == transition and judgment.due is None and not judgment.start
    assert judgment.fields[task_row.FINISHED_AT] == format_utc(NOW) and len(judgment.fields[task_row.REASONS]) == 1
    # The skip origin comes from the transition table, which the write takes it from (dispatch_writes).
    assert transitions.task_transition(transition).skip_origin == skip_origin
    assert_dispatcher_may(S.WAITING.value, judgment)


def test_a_rule_the_engine_does_not_know_fails_the_row_with_the_cause():
    judgment = dispatch.judge(row(rule="always"), {}, RUN, now=NOW)
    assert judgment.transition == "fail_wait" and "always" in judgment.cause
    assert judgment.fields[task_row.ERROR] == judgment.cause


def test_a_missing_upstream_row_fails_the_row_with_a_warning():
    """Invariant 11: the dispatcher fails each waiting dependant of a missing row."""
    judgment = dispatch.judge(row(deps=["extract", "audit"]), {"audit": up("audit", S.SUCCESS.value)}, RUN, now=NOW)
    assert judgment.transition == "fail_wait"
    assert judgment.cause == "the row of its upstream extract is missing" and "load:" in judgment.warning


# ── a waiting row: its facts and its limits ─────────────────────────────────

def test_its_facts_are_evaluated_only_once_its_upstreams_allow_it():
    waiting = row(deps=["extract"], wait_for=True)
    assert not dispatch.needs_assets(waiting, {"extract": row("extract")})
    assert dispatch.needs_assets(waiting, {"extract": up("extract", S.SUCCESS.value)})
    assert not dispatch.needs_assets(waiting, {})                                         # a missing upstream
    assert not dispatch.needs_assets(row(wait_for=False), {})


def test_a_row_waiting_for_facts_records_them_and_falls_due_at_the_7_day_limit():
    judgment = dispatch.judge(row(wait_for=True), {}, RUN, now=NOW, assets=facts(False))
    assert judgment.fields[task_row.REASONS] == ["lake/prices: no fact for 2026-11-19"]
    assert judgment.fields[task_row.ENABLED_AT] == format_utc(WAITING_SINCE)
    assert judgment.due == WAITING_SINCE + deadlines.WAIT_FOR_LIMIT


def test_a_wait_for_facts_past_its_limit_fails_the_row_whose_error_replaces_its_reasons():
    waiting = row(wait_for=True, enabled_at=WAITING_SINCE, reasons=["lake/prices: no fact for 2026-11-19"])
    judgment = dispatch.judge(waiting, {}, RUN, now=WAITING_SINCE + deadlines.WAIT_FOR_LIMIT, assets=facts(False))
    assert judgment.transition == "fail_wait" and "waited past its limit" in judgment.cause
    assert judgment.removed == (task_row.REASONS,) and judgment.fields[task_row.ERROR] == judgment.cause


def test_an_orchestration_timeout_limits_the_whole_wait_from_its_start():
    waiting = row(deps=["extract"], orchestration_timeout=timedelta(minutes=30))
    judgment = dispatch.judge(waiting, {"extract": row("extract")}, RUN, now=NOW)
    assert judgment.transition == "fail_wait" and format_utc(WAITING_SINCE + timedelta(minutes=30)) in judgment.cause
    longer = row(deps=["extract"], orchestration_timeout=timedelta(hours=2))
    assert dispatch.judge(longer, {"extract": row("extract")}, RUN, now=NOW).due == WAITING_SINCE + timedelta(hours=2)


def test_facts_that_arrived_start_the_row():
    judgment = dispatch.judge(row(wait_for=True), {}, RUN, now=NOW, assets=facts(True, enabled_at=NOW))
    assert (judgment.transition, judgment.start) == ("ready", True)


# ── a countdown ─────────────────────────────────────────────────────────────

def test_a_ready_row_with_wait_before_counts_down_from_when_it_became_ready():
    judgment = dispatch.judge(row(wait_before=7200, enabled_at=WAITING_SINCE), {}, RUN, now=NOW)
    end = WAITING_SINCE + timedelta(hours=2)
    assert (judgment.transition, judgment.due) == (None, end)
    assert judgment.fields == {task_row.COUNTDOWN_END: format_utc(end), task_row.REASONS: [dispatch.COUNTING_DOWN]}


def test_its_facts_count_toward_when_it_became_ready():
    judgment = dispatch.judge(row(wait_for=True, wait_before=600), {}, RUN, now=NOW,
                              assets=facts(True, enabled_at=NOW - timedelta(minutes=5)))
    assert judgment.due == NOW + timedelta(minutes=5)


def test_a_countdown_already_over_starts_the_row_at_once():
    judgment = dispatch.judge(row(wait_before=60, enabled_at=WAITING_SINCE), {}, RUN, now=NOW)
    assert (judgment.transition, judgment.start) == ("ready", True)


def test_a_row_is_not_decided_again_during_its_countdown():
    counting = row(deps=["extract"], wait_before=7200, countdown_end=NOW + timedelta(minutes=10))
    judgment = dispatch.judge(counting, {"extract": row("extract")}, RUN, now=NOW)       # its upstream restarted
    assert (judgment.transition, judgment.fields, judgment.due) == (None, {}, NOW + timedelta(minutes=10))
    assert not dispatch.needs_assets(row(wait_for=True, countdown_end=NOW), {})


def test_at_its_end_it_starts_from_its_upstreams_alone():
    counting = row(deps=["extract"], wait_for=True, countdown_end=NOW, reasons=[dispatch.COUNTING_DOWN])
    judgment = dispatch.judge(counting, {"extract": up("extract", S.SUCCESS.value)}, RUN, now=NOW)
    assert (judgment.transition, judgment.start) == ("ready", True)                     # no facts were read
    assert set(judgment.removed) == {task_row.REASONS, task_row.COUNTDOWN_END}


def test_a_countdown_whose_readiness_went_is_removed():
    counting = row(deps=["extract"], countdown_end=NOW - timedelta(minutes=1), reasons=[dispatch.COUNTING_DOWN])
    judgment = dispatch.judge(counting, {"extract": row("extract")}, RUN, now=NOW)
    assert judgment.transition is None and task_row.COUNTDOWN_END in judgment.removed
    assert judgment.fields == {task_row.REASONS: ["waiting for its upstreams"]}


# ── a pause ─────────────────────────────────────────────────────────────────

def test_a_paused_run_holds_its_ready_rows_and_starts_none():
    judgment = dispatch.judge(row(reasons=["waiting for its upstreams"]), {}, PAUSED, now=NOW)
    assert (judgment.transition, judgment.start, judgment.due) == ("hold", False, None)
    assert judgment.removed == (task_row.REASONS,)
    assert_dispatcher_may(S.WAITING.value, judgment)
    held = row(status=S.WAITING_PAUSED.value, enabled_at=WAITING_SINCE)
    assert dispatch.judge(held, {}, PAUSED, now=NOW) == dispatch.Judgment("load")


def test_no_limit_falls_due_or_fires_in_a_pause():
    waiting = row(wait_for=True, enabled_at=WAITING_SINCE - deadlines.WAIT_FOR_LIMIT)
    judgment = dispatch.judge(waiting, {}, PAUSED, now=NOW, assets=facts(False))
    assert (judgment.transition, judgment.due) == (None, None)


def test_a_pause_does_not_stop_a_countdown_and_its_end_in_a_pause_holds_the_row():
    counting = row(countdown_end=NOW + timedelta(minutes=5))
    assert dispatch.judge(counting, {}, PAUSED, now=NOW).due == NOW + timedelta(minutes=5)
    ended = dispatch.judge(row(countdown_end=NOW), {}, PAUSED, now=NOW)
    assert ended.transition == "hold"


def test_the_resume_starts_a_held_row_and_releases_one_that_is_no_longer_ready():
    held = row(status=S.WAITING_PAUSED.value, countdown_end=NOW - timedelta(minutes=3))
    started = dispatch.judge(held, {}, RUN, now=NOW)
    assert (started.transition, started.start) == ("ready", True)
    assert_dispatcher_may(S.WAITING_PAUSED.value, started)
    stale = row(status=S.WAITING_PAUSED.value, deps=["extract"])
    released = dispatch.judge(stale, {"extract": row("extract")}, RUN, now=NOW)
    assert released.transition == "release" and released.fields[task_row.STATUS_SINCE] == format_utc(NOW)
    assert_dispatcher_may(S.WAITING_PAUSED.value, released)


# ── a start ─────────────────────────────────────────────────────────────────

def ready_row(**times: Any) -> dispatch.RowView:
    return row(status=S.DEPS_READY.value, status_since=NOW - timedelta(minutes=2), **times)


def test_a_start_in_flight_waits_for_its_check():
    judgment = dispatch.judge(ready_row(), {}, RUN, now=NOW)
    assert (judgment.start, judgment.due) == (False, NOW - timedelta(minutes=2) + attention.BACKOFF[0])


def test_a_failed_start_is_tried_again_at_its_next_try():
    failed = ready_row(start_failed_first=NOW - timedelta(minutes=1), start_failed_last=NOW - timedelta(seconds=20),
                       start_failures=1)
    waiting = dispatch.judge(failed, {}, RUN, now=NOW)
    assert (waiting.start, waiting.due) == (False, NOW + timedelta(seconds=10))     # 30 s after the last failure
    assert dispatch.judge(failed, {}, RUN, now=NOW + timedelta(seconds=10)).start


def test_a_start_that_kept_failing_6_hours_fails_the_row_with_its_last_error():
    failing = row(status=S.DEPS_READY.value, start_failed_first=NOW - deadlines.START_FAIL_AFTER,
                  start_failed_last=NOW - timedelta(minutes=5), start_failures=40, error="ThrottlingException")
    judgment = dispatch.judge(failing, {}, RUN, now=NOW)
    assert judgment.transition == "fail_start" and "ThrottlingException" in judgment.cause
    assert_dispatcher_may(S.DEPS_READY.value, judgment)


def test_a_paused_run_starts_no_row_not_even_a_failed_start():
    failed = ready_row(start_failed_first=NOW - timedelta(minutes=5), start_failed_last=NOW - timedelta(minutes=5),
                       start_failures=1)
    assert dispatch.judge(failed, {}, PAUSED, now=NOW) == dispatch.Judgment("load")


def test_a_transient_failed_start_counts_toward_its_deadlines():
    judgment = dispatch.start_failed(ready_row(), RUN, now=NOW, error="ThrottlingException", kind=dispatch.TRANSIENT)
    assert judgment.fields == {task_row.START_FAILED_FIRST: format_utc(NOW), task_row.START_FAILED_LAST: format_utc(NOW),
                               task_row.START_FAILURES: 1, task_row.ERROR: "ThrottlingException"}
    assert judgment.due == NOW + deadlines.START_BACKOFF[0] and judgment.warning is None


def test_the_first_failed_attempt_past_15_minutes_warns_once():
    first = NOW - deadlines.START_WARN_AFTER - timedelta(minutes=1)
    before = ready_row(start_failed_first=first, start_failed_last=NOW - timedelta(minutes=5), start_failures=6)
    warned = dispatch.start_failed(before, RUN, now=NOW, error="5xx", kind=dispatch.TRANSIENT)
    assert "has failed to start since" in warned.warning
    after = ready_row(start_failed_first=first, start_failed_last=NOW - timedelta(seconds=30), start_failures=7)
    assert dispatch.start_failed(after, RUN, now=NOW, error="5xx", kind=dispatch.TRANSIENT).warning is None


def test_a_failed_attempt_past_6_hours_fails_the_row():
    late = ready_row(start_failed_first=NOW - deadlines.START_FAIL_AFTER, start_failed_last=NOW - timedelta(minutes=5),
                     start_failures=40)
    judgment = dispatch.start_failed(late, RUN, now=NOW, error="5xx", kind=dispatch.TRANSIENT)
    assert judgment.transition == "fail_start" and "5xx" in judgment.cause


def test_an_access_denied_start_warns_and_counts_toward_no_deadline():
    judgment = dispatch.start_failed(ready_row(), RUN, now=NOW, error="AccessDeniedException", kind=dispatch.DENIED)
    assert judgment.transition is None and judgment.fields == {} and "access denied" in judgment.warning


def test_a_start_refused_for_good_fails_the_row():
    judgment = dispatch.start_failed(ready_row(), RUN, now=NOW, error="input of 300000 bytes", kind=dispatch.REFUSED)
    assert judgment.transition == "fail_start" and "300000 bytes" in judgment.cause
    with pytest.raises(ValueError, match="unknown kind"):
        dispatch.start_failed(ready_row(), RUN, now=NOW, error="x", kind="other")


# ── a decision, a running row ───────────────────────────────────────────────

def test_a_row_waiting_for_a_decision_fails_at_its_deadline_which_a_pause_holds():
    asking = row(status=S.WAITING_DECISION.value, decision_since=NOW - timedelta(hours=5),
                 decision_timeout=timedelta(hours=5))
    judgment = dispatch.judge(asking, {}, RUN, now=NOW)
    assert judgment.transition == "fail_undecided" and "decision timeout" in judgment.cause
    assert_dispatcher_may(S.WAITING_DECISION.value, judgment)
    assert dispatch.judge(asking, {}, RUN, now=NOW - timedelta(minutes=1)).due == NOW
    assert dispatch.judge(asking, {}, PAUSED, now=NOW).due == deadlines.FAR_FUTURE


def test_a_running_row_keeps_its_check():
    running = row(status=S.RUNNING.value, status_since=NOW - timedelta(minutes=30), checks=2,
                  last_check=NOW - timedelta(minutes=5))
    assert dispatch.judge(running, {}, RUN, now=NOW) == dispatch.Judgment("load", due=NOW + timedelta(minutes=25))


# ── a stop ──────────────────────────────────────────────────────────────────

def test_a_stop_stops_every_unfinished_row_and_leaves_final_ones():
    judgment = dispatch.stop(row(status=S.RUNNING.value), now=NOW, cause="stopped by ops: wrong date")
    assert judgment.transition == "stop" and judgment.fields[task_row.ERROR] == "stopped by ops: wrong date"
    assert_dispatcher_may(S.RUNNING.value, judgment)
    assert dispatch.stop(row(status=S.SUCCESS.value), now=NOW, cause="x") == dispatch.Judgment("load")


def test_a_stop_writes_dependants_before_their_upstreams():
    rows = {"extract": row("extract"), "load": row("load", deps=["extract"]),
            "report": row("report", deps=["load", "extract"]), "audit": row("audit")}
    order = dispatch.stop_order(rows)
    assert order.index("report") < order.index("load") < order.index("extract") and set(order) == set(rows)


def _chain(n: int) -> Dict[str, dispatch.RowView]:
    return {f"t{i:05d}": row(f"t{i:05d}", deps=[f"t{i - 1:05d}"] if i else []) for i in range(n)}


def test_a_pass_orders_and_stops_a_chain_of_2000_without_recursion():
    """polyris/CLAUDE.md, "Graph traversal": the gate is a recursion limit of 100 (the review of TR-4)."""
    import sys

    rows = _chain(2000)
    dependencies = {name: r.dependencies for name, r in rows.items()}
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        forward = dispatch.upstreams_first(set(rows), dependencies)
        backward = dispatch.stop_order(rows)
    finally:
        sys.setrecursionlimit(limit)
    assert forward == sorted(rows) and backward == sorted(rows, reverse=True)


def test_a_pass_decides_each_row_after_its_upstreams_among_those_it_decides():
    dependencies = {"report": ["load", "extract"], "load": ["extract"], "extract": [], "audit": ["missing"]}
    order = dispatch.upstreams_first({"report", "load", "extract", "audit"}, dependencies)
    assert order.index("extract") < order.index("load") < order.index("report") and set(order) == set(dependencies)
    assert dispatch.upstreams_first({"report", "load"}, dependencies) == ["load", "report"]    # extract not decided


def test_a_cycle_no_deploy_makes_still_orders_every_row_once():
    rows = {"a": row("a", deps=["b"]), "b": row("b", deps=["a"]), "c": row("c")}
    assert sorted(dispatch.stop_order(rows)) == ["a", "b", "c"]
    assert sorted(dispatch.upstreams_first(set(rows), {n: r.dependencies for n, r in rows.items()})) == ["a", "b", "c"]


def test_a_waiting_row_a_pass_cannot_decide_fails_with_the_cause_and_warns():
    """ADR #137 §3: fail_wait on a CoordinationError, so the run goes on (Rule 51)."""
    judgment = dispatch.undecidable(row(), now=NOW, cause="it could not be decided: ValueError: no partition")
    assert judgment.transition == "fail_wait" and judgment.status == S.FAILED.value
    assert judgment.fields[task_row.ERROR] == "it could not be decided: ValueError: no partition"
    assert judgment.warning == "load: it could not be decided: ValueError: no partition"
    assert_dispatcher_may(S.WAITING.value, judgment)


def test_a_ready_row_a_pass_cannot_start_fails_with_the_cause_and_warns():
    """ADR #137 §6, level 2: every attempt would meet the same error, so the start counts as refused for good. Before,
    the row stayed deps_ready, and each liveness check started it again and warned (the review of the branch, P-4)."""
    judgment = dispatch.undecidable(row(status=S.DEPS_READY.value), now=NOW,
                                    cause="it could not be decided: SpecError: the spec text of load is missing")
    assert judgment.transition == "fail_start" and judgment.status == S.FAILED.value
    assert judgment.fields[task_row.ERROR] == "it could not be decided: SpecError: the spec text of load is missing"
    assert judgment.warning == "load: it could not be decided: SpecError: the spec text of load is missing"
    assert_dispatcher_may(S.DEPS_READY.value, judgment)


@pytest.mark.parametrize("status", [S.RUNNING.value, S.WAITING_DECISION.value])
def test_any_other_row_a_pass_cannot_decide_keeps_its_state_and_warns(status):
    due = NOW + timedelta(minutes=10)
    view = dispatch.RowView(**{**row(status=status).__dict__, "due": due})
    judgment = dispatch.undecidable(view, now=NOW, cause="it could not be decided: KeyError: 'x'")
    assert (judgment.transition, judgment.fields, judgment.due) == (None, {}, due)
    assert judgment.warning == "load: it could not be decided: KeyError: 'x'"


# ── the run ─────────────────────────────────────────────────────────────────

def test_a_run_with_an_unfinished_row_has_no_outcome_yet():
    end = dispatch.run_outcome(RUN, {"extract": S.SUCCESS.value, "load": S.RUNNING.value, "report": S.WAITING.value})
    assert (end.finished, end.summary) == (False, {"success": 1, "running": 1, "waiting": 1})


def test_a_run_whose_rows_are_final_has_its_outcome_and_a_failed_runs_cause():
    assert dispatch.run_outcome(RUN, {t: S.SUCCESS.value for t in ("a", "b", "c")}).outcome == "success"
    end = dispatch.run_outcome(RUN, {"a": S.SUCCESS.value, "b": S.FAILED.value, "c": S.UPSTREAM_FAILED.value})
    assert end.outcome == "failed" and end.cause == "2 tasks failed: b, c"


def test_a_missing_row_counts_as_failed_for_the_runs_end():
    end = dispatch.run_outcome(RUN, {"a": S.SUCCESS.value, "b": None, "c": S.SKIPPED.value})
    assert (end.outcome, end.missing) == ("failed", ("b",)) and "b" in end.cause


def test_a_failed_direct_step_fails_the_run():
    marked = dispatch.RunView(**{**RUN.__dict__, "step_failure": {"step": "notify", "error": "SNS.NotFound"}})
    end = dispatch.run_outcome(marked, {t: S.SUCCESS.value for t in ("a", "b", "c")})
    assert end.outcome == "failed" and end.cause.startswith("step notify failed")


def test_the_answer_and_the_summary_are_written_only_when_needed():
    end = dispatch.run_outcome(RUN, {t: S.SUCCESS.value for t in ("a", "b", "c")})
    current = dispatch.RunView(**{**RUN.__dict__, "token_order": 2, "answered_order": None})
    assert dispatch.needs_answer(current, end) and dispatch.summary_changed(current, end)
    done = dispatch.RunView(**{**current.__dict__, "answered_order": 2, "summary": {"success": 3}})
    assert not dispatch.needs_answer(done, end) and not dispatch.summary_changed(done, end)
    newer = dispatch.RunView(**{**done.__dict__, "token_order": 100_000_000})        # a redrive's token
    assert dispatch.needs_answer(newer, end)


# ── reading the rows back ───────────────────────────────────────────────────

def test_a_row_start_run_wrote_reads_back_as_the_pass_judges_it():
    def spec(**fields: Any) -> task_spec.TaskSpec:
        base = {"task_id": "t", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"}
        text = task_spec.canonical_json({**base, **fields})
        return task_spec.parse(text, expected_hash=task_spec.text_hash(text))

    call = start_run.call_from({"format_version": task_spec.FORMAT_VERSION, "spec_id": "orders#" + "f" * 64,
                                "task_count": 1, "pipeline_name": "orders", "execution_name": "orders-run-1",
                                "execution_arn": "arn:aws:states:us-east-1:123456789012:execution:orders:orders-run-1", "execution_start": "2026-11-19T06:00:00.000Z", "token": "t", "retry_count": 0,
                                "date": "2026-11-19"})
    [item] = start_run.task_items(call, {"load": spec(task_id="load", dependencies=["extract"], wait_before=600,
                                                      trigger_rule="one_success", wait_for=[{"asset_name": "a"}],
                                                      orchestration_timeout=86400)}, now=NOW)
    view = dispatch.row_view(item)
    assert (view.task_id, view.status, view.generation, view.trigger_rule) == ("load", "waiting", 1, "one_success")
    assert (view.dependencies, view.has_wait_for, view.wait_before) == (("extract",), True, timedelta(minutes=10))
    assert view.times.orchestration_timeout == timedelta(days=1) and view.due == NOW + timedelta(days=1)


def test_a_run_row_reads_back_as_the_pass_judges_it():
    item: Dict[str, Any] = {run_row.TABLE_KEY: "_run_orders", run_row.EXECUTION: "orders", run_row.STATUS: "active",
                            run_row.RUN_VERSION: 7, run_row.TASK_COUNT: 3, run_row.TOKEN_ORDER: 2,
                            run_row.ANSWERED: 2, run_row.SUMMARY: {"success": 3},
                            run_row.PAUSES: [[1763553600]], run_row.STOP: {"by": "ops"}, run_row.RUN_DATE: "2026-11-19",
                            run_row.LEFT_AT: "2026-11-19T12:00:00.000Z", run_row.REQUESTS: {"pointers": {"by": "x"}}}
    view = dispatch.run_view(item)
    assert (view.version, view.task_count, view.answered, view.paused, view.stopping) == (7, 3, True, True, True)
    assert (view.date, view.left_at, view.requests) == ("2026-11-19", "2026-11-19T12:00:00.000Z",
                                                         {"pointers": {"by": "x"}})
    assert view.active and view.summary == {"success": 3}
    assert dispatch.run_view({**item, run_row.REQUESTS: {"pointers": "odd"}}).requests == {}   # no map: none
    assert not dispatch.run_view({**item, run_row.STOP_DONE: True}).stopping


def test_a_judgment_says_when_a_row_leaves_its_wait():
    assert dispatch.Judgment("load", "ready").leaves_wait
    assert not dispatch.Judgment("load", "hold").leaves_wait and not dispatch.Judgment("load").leaves_wait


def test_a_run_row_reads_back_with_its_pipeline_and_its_start():
    item = {run_row.TABLE_KEY: "_run_orders", run_row.PIPELINE: "orders", run_row.STARTED_AT: "2026-11-19T06:00:00.000Z"}
    view = dispatch.run_view(item)
    assert (view.pipeline, view.started_at) == ("orders", datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc))
    assert dispatch.run_view({run_row.TABLE_KEY: "_run_orders"}).started_at is None


# ── the rows that a 1.2 run left in flight (*Migration*, invariant 11) ──────

def test_a_row_without_readiness_fields_reads_back_for_its_stop_alone():
    legacy = dispatch.stop_view({task_row.TABLE_KEY: "load-k", task_row.STATUS: S.WAITING.value})
    assert (legacy.task_id, legacy.status, legacy.generation) == ("load-k", S.WAITING.value, 0)
    raised = dispatch.stop_view({task_row.TABLE_KEY: "load-k", task_row.TASK_NAME: "load",
                                 task_row.STATUS: S.WAITING.value, task_row.GENERATION: 2})
    assert (raised.task_id, raised.generation) == ("load", 2)
    assert dispatch.stop(legacy, now=NOW, cause="its run row is missing").transition == "stop"
    with pytest.raises(ValueError, match="readiness fields"):
        dispatch.row_view({task_row.TABLE_KEY: "load-k", task_row.STATUS: S.WAITING.value})


def test_a_row_of_13_reads_back_for_its_stop_as_any_pass_reads_it():
    item = {task_row.TABLE_KEY: "load-k", task_row.TASK_NAME: "load", task_row.STATUS: S.WAITING.value,
            task_row.GENERATION: 1, task_row.READINESS: task_spec.readiness_text(task_spec.parse(_SPEC_TEXT,
                                                                                            expected_hash=_SPEC_HASH))}
    assert dispatch.stop_view(item) == dispatch.row_view(item)


_SPEC_TEXT = task_spec.canonical_json({"task_id": "load", "task_type": "sfn", "dependencies": [],
                                       "trigger_rule": "all_success"})
_SPEC_HASH = task_spec.text_hash(_SPEC_TEXT)


# ── lost hints: when a row's wait could have ended (§7) ─────────────────────

def test_a_row_made_ready_by_its_upstreams_could_start_from_when_they_allowed_it():
    judgment = dispatch.judge(row(deps=["extract"]), {"extract": up("extract", S.SUCCESS.value)}, RUN, now=NOW)
    assert judgment.since == NOW - timedelta(minutes=20)


def test_a_row_made_ready_by_its_facts_could_start_from_the_latest_of_both():
    judgment = dispatch.judge(row(wait_for=True, enabled_at=WAITING_SINCE), {}, RUN, now=NOW,
                              assets=facts(True, enabled_at=NOW - timedelta(minutes=7)))
    assert (judgment.transition, judgment.since) == ("ready", NOW - timedelta(minutes=7))


@pytest.mark.parametrize("upstream,origin,transition", [(S.FAILED.value, None, "block"),
                                                        (S.SKIPPED.value, SkipOrigin.RULE, "skip_by_rule")])
def test_a_row_its_upstreams_decided_could_be_decided_from_when_they_became_final(upstream, origin, transition):
    judgment = dispatch.judge(row(deps=["extract"]), {"extract": up("extract", upstream, origin=origin)}, RUN, now=NOW)
    assert (judgment.transition, judgment.since) == (transition, NOW - timedelta(minutes=20))


def test_a_countdowns_end_a_hold_a_failure_and_a_wait_give_no_such_time():
    """The sweeper is how a countdown ends; a hold or a wait does not end the wait; a failure is no hint's to give."""
    counted = row(deps=["extract"], countdown_end=NOW, reasons=[dispatch.COUNTING_DOWN])
    assert dispatch.judge(counted, {"extract": up("extract", S.SUCCESS.value)}, RUN, now=NOW).since is None
    assert dispatch.judge(row(), {}, PAUSED, now=NOW).since is None
    assert dispatch.judge(row(deps=["extract"]), {"extract": row("extract")}, RUN, now=NOW).since is None
    assert dispatch.judge(row(deps=["extract"]), {"extract": None}, RUN, now=NOW).since is None


# ── a start that went through after failed attempts (§1, §5) ───────────────

def test_a_start_that_went_through_after_failures_counts_its_liveness_from_now():
    failing = row(status=S.DEPS_READY.value, error="ThrottlingException", start_failures=2,
                  start_failed_first=NOW - timedelta(minutes=3), start_failed_last=NOW - timedelta(minutes=1),
                  status_since=NOW - timedelta(minutes=4))
    judgment = dispatch.started(failing, RUN, now=NOW)
    assert judgment.fields == {task_row.STATUS_SINCE: format_utc(NOW)}
    assert set(judgment.removed) == {task_row.START_FAILED_FIRST, task_row.START_FAILED_LAST, task_row.START_FAILURES,
                                     task_row.ERROR}
    assert judgment.due == NOW + attention.BACKOFF[0]


@pytest.mark.parametrize("status", [S.DEPS_READY.value, S.RUNNING.value])
def test_any_other_start_changes_nothing(status):
    started = row(status=status, status_since=NOW)
    assert dispatch.started(started, RUN, now=NOW) == dispatch.Judgment("load", due=started.due)


# ── liveness: the sweeper's judgment of a started row's execution (§1, §3) ──

CHECKED = NOW - timedelta(minutes=10)


@pytest.mark.parametrize("status", [S.DEPS_READY.value, S.RUNNING.value])
def test_a_started_row_is_judged_once_its_liveness_check_has_come(status):
    assert dispatch.liveness_due(row(status=status, status_since=CHECKED), RUN, now=NOW)
    assert not dispatch.liveness_due(row(status=status, status_since=CHECKED), RUN, now=NOW - timedelta(seconds=1))
    assert not dispatch.liveness_due(row(status=status), RUN, now=NOW)                  # no time to count from


def test_a_ready_row_whose_start_failed_or_that_a_pause_holds_is_not_judged():
    failing = row(status=S.DEPS_READY.value, status_since=CHECKED, start_failures=1, start_failed_first=CHECKED,
                  start_failed_last=CHECKED)
    assert not dispatch.liveness_due(failing, RUN, now=NOW)
    assert not dispatch.liveness_due(row(status=S.DEPS_READY.value, status_since=CHECKED), PAUSED, now=NOW)
    assert dispatch.liveness_due(row(status=S.RUNNING.value, status_since=CHECKED), PAUSED, now=NOW)
    assert not dispatch.liveness_due(row(status_since=CHECKED), RUN, now=NOW)            # a waiting row


def test_the_end_of_a_run_task_brings_the_check_of_its_row_now_and_changes_nothing_else():
    """ADR #137 §7: `at_once` moves only the time of the check; a failed start, a pause and a row that waits for
    no outcome keep theirs."""
    young = NOW - timedelta(seconds=30)
    for status in (S.DEPS_READY.value, S.RUNNING.value):
        assert not dispatch.liveness_due(row(status=status, status_since=young), RUN, now=NOW)
        assert dispatch.liveness_due(row(status=status, status_since=young), RUN, now=NOW, at_once=True)
        assert dispatch.liveness_due(row(status=status), RUN, now=NOW, at_once=True)    # no time to count from
    failing = row(status=S.DEPS_READY.value, status_since=young, start_failures=1, start_failed_first=young,
                  start_failed_last=young)
    assert not dispatch.liveness_due(failing, RUN, now=NOW, at_once=True)
    assert not dispatch.liveness_due(row(status=S.DEPS_READY.value, status_since=young), PAUSED, now=NOW, at_once=True)
    assert dispatch.liveness_due(row(status=S.RUNNING.value, status_since=young), PAUSED, now=NOW, at_once=True)
    for status in (S.WAITING.value, S.SUCCESS.value, S.WAITING_DECISION.value):
        assert not dispatch.liveness_due(row(status=status, status_since=young), RUN, now=NOW, at_once=True)


@pytest.mark.parametrize("status", ["RUNNING", "PENDING_REDRIVE"])
def test_an_execution_that_runs_records_the_check_and_backs_off(status):
    judgment = dispatch.liveness(row(status=S.RUNNING.value, status_since=CHECKED, checks=0), RUN, now=NOW,
                                 execution=dispatch.Execution(status))
    assert (judgment.transition, judgment.start) == (None, False)
    assert judgment.fields == {task_row.LAST_CHECK: format_utc(NOW), task_row.CHECKS: 1}
    assert judgment.due == NOW + timedelta(minutes=20)


@pytest.mark.parametrize("execution,said", [
    (dispatch.Execution("FAILED", "States.Runtime", "the Lambda was deleted"),
     "FAILED (States.Runtime: the Lambda was deleted)"),
    (dispatch.Execution("ABORTED"), "ABORTED"),
    (dispatch.Execution("TIMED_OUT", None, "the state machine's timeout"), "TIMED_OUT (the state machine's timeout)"),
    (dispatch.Execution("SUCCEEDED"), "SUCCEEDED"),
])
@pytest.mark.parametrize("status", [S.DEPS_READY.value, S.RUNNING.value])
def test_an_execution_that_ended_without_an_outcome_fails_its_row_with_how_it_ended(execution, said, status):
    judgment = dispatch.liveness(row(status=status, status_since=CHECKED), RUN, now=NOW, execution=execution)
    assert (judgment.transition, judgment.status) == ("fail_dead", S.FAILED.value)
    assert judgment.cause == f"its run_task execution ended {said} without recording an outcome"
    assert_dispatcher_may(status, judgment)


@pytest.mark.parametrize("status", [S.DEPS_READY.value, S.RUNNING.value])
def test_an_execution_not_visible_yet_is_no_dead_one(status):
    """Invariant 10: DescribeExecution lags; within sweep.EXECUTION_VISIBLE_AFTER, not found is not gone."""
    recent = row(status=status, status_since=NOW - sweep.EXECUTION_VISIBLE_AFTER + timedelta(seconds=1))
    judgment = dispatch.liveness(recent, RUN, now=NOW, execution=dispatch.Execution(None))
    assert (judgment.transition, judgment.start, judgment.fields[task_row.CHECKS]) == (None, False, 1)
    unknown_since = dispatch.liveness(row(status=status), RUN, now=NOW, execution=dispatch.Execution(None))
    assert (unknown_since.transition, unknown_since.start) == (None, False)


def test_a_ready_row_whose_execution_does_not_exist_is_started_again():
    """Invariant 3: the row is deps_ready already; the check's record is written first, then the start."""
    judgment = dispatch.liveness(row(status=S.DEPS_READY.value, status_since=CHECKED), RUN, now=NOW,
                                 execution=dispatch.Execution(None))
    assert (judgment.transition, judgment.start, judgment.fields[task_row.CHECKS]) == (None, True, 1)


def test_a_running_row_whose_execution_does_not_exist_fails():
    judgment = dispatch.liveness(row(status=S.RUNNING.value, status_since=CHECKED), RUN, now=NOW,
                                 execution=dispatch.Execution(None))
    assert (judgment.transition, judgment.cause) == ("fail_dead", "its run_task execution does not exist")


def test_an_execution_says_how_it_ended():
    assert dispatch.Execution(None).account() == "missing" and not dispatch.Execution(None).ended
    assert dispatch.Execution("FAILED", "E", "C").account() == "FAILED (E: C)" and dispatch.Execution("FAILED").ended


# ── the engine's stop of a run (§4) ─────────────────────────────────────────

STARTED = NOW - timedelta(hours=2)
LIVE = dispatch.RunView(key="_run_orders", execution="orders", status=run_row.ACTIVE, version=3, task_count=2,
                        token="t", token_order=1, started_at=STARTED, pipeline="orders")
CREATING = dispatch.RunView(**{**LIVE.__dict__, "status": run_row.CREATING})
ENDED = dispatch.Execution("ABORTED", None, "stopped in the AWS console")


def test_an_active_run_whose_execution_ended_before_its_answer_is_stopped_with_how_it_ended():
    assert dispatch.engine_check_due(LIVE, now=NOW, finished=False)
    assert dispatch.engine_stop_cause(LIVE, now=NOW, execution=ENDED, finished=False) == (
        "its execution ended ABORTED (stopped in the AWS console) before the run's answer")


def test_an_active_run_whose_execution_runs_goes_on():
    for status in ("RUNNING", "PENDING_REDRIVE"):
        assert dispatch.engine_stop_cause(LIVE, now=NOW, execution=dispatch.Execution(status), finished=False) is None


def test_an_active_run_whose_execution_does_not_exist_is_stopped_once_it_would_be_visible():
    young = dispatch.RunView(**{**LIVE.__dict__, "started_at": NOW - timedelta(minutes=1)})
    assert dispatch.engine_stop_cause(young, now=NOW, execution=dispatch.Execution(None), finished=False) is None
    assert dispatch.engine_stop_cause(LIVE, now=NOW, execution=dispatch.Execution(None), finished=False) == (
        "its execution does not exist")


def test_a_run_still_creating_after_start_runs_retry_window_never_became_active():
    window = start_run.retry_window()
    young = dispatch.RunView(**{**CREATING.__dict__, "started_at": NOW - window + timedelta(seconds=1)})
    assert not dispatch.engine_check_due(young, now=NOW, finished=False)
    assert dispatch.engine_stop_cause(young, now=NOW, execution=ENDED, finished=False) is None
    assert dispatch.engine_check_due(CREATING, now=NOW, finished=False)
    assert dispatch.engine_stop_cause(CREATING, now=NOW, execution=dispatch.Execution("RUNNING"), finished=False) == (
        f"Start_Run did not make the run active within its retry window of {window}")
    assert dispatch.engine_stop_cause(CREATING, now=NOW, execution=ENDED, finished=False) == (
        f"Start_Run did not make the run active within its retry window of {window}; its execution ABORTED "
        "(stopped in the AWS console)")
    assert dispatch.engine_stop_cause(CREATING, now=NOW, execution=dispatch.Execution(None), finished=False).endswith(
        "; its execution missing")


@pytest.mark.parametrize("run,finished", [
    (dispatch.RunView(**{**LIVE.__dict__, "stop": {"by": "ops"}}), False),         # a Stop of the run keeps who asked
    (dispatch.RunView(**{**LIVE.__dict__, "answered_order": 1}), False),           # its answer is marked
    (LIVE, True),                                                                  # its end answers it
    (dispatch.RunView(**{**LIVE.__dict__, "started_at": None}), False),            # a step's mark alone
    (dispatch.RunView(**{**LIVE.__dict__, "status": ""}), False),
])
def test_no_run_is_stopped_that_its_own_end_or_a_stop_already_covers(run, finished):
    assert not dispatch.engine_check_due(run, now=NOW, finished=finished)
    assert dispatch.engine_stop_cause(run, now=NOW, execution=ENDED, finished=finished) is None


def test_an_execution_that_could_not_be_read_stops_nothing():
    """Invariant 10: no read failure leads to a close."""
    assert dispatch.engine_stop_cause(LIVE, now=NOW, execution=None, finished=False) is None
    assert dispatch.engine_stop_cause(CREATING, now=NOW, execution=None, finished=False) is None
