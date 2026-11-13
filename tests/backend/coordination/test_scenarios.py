"""Coordination scenarios (ADR #130, Testing).

Each scenario checks both sides of a wake: the task starts once its
conditions hold, and never before. They speak only the World API; a
scenario whose code path has not landed yet is marked needs_binding(stage)
and skipped (stages.py). Defect ids refer to defects.py.
"""

import itertools

import pytest
from dags import asset, chain, pipeline, waits_for

DATE = "2026-11-12"
defect = pytest.mark.defect
needs_binding = pytest.mark.needs_binding

A, B, C = asset("ns/a"), asset("ns/b"), asset("ns/c")
WEEKLY = asset("ns/weekly", granularity="weekly")


# ── task dependencies ───────────────────────────────────────────────────


def test_a_root_task_starts_at_registration(world):
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    assert world.running_tasks(run) == ["extract"]
    world.complete(run, "extract")
    assert world.status(run, "extract") == "success"
    assert world.pipeline_status(run) == "SUCCEEDED"


def test_a_downstream_task_starts_only_after_its_upstream_succeeds(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    assert world.running_tasks(run) == ["extract"]
    assert world.status(run, "load") == "waiting"
    world.complete(run, "extract")
    assert world.running_tasks(run) == ["load"]
    assert len(world.signals(run, "load")) == 1


# ── pull: wait_for ──────────────────────────────────────────────────────

STEPS = ("register", "write a", "write b", "hint a", "hint b")


def _orders():
    """Every order of STEPS in which a hint follows its write."""
    for order in itertools.permutations(STEPS):
        if order.index("write a") < order.index("hint a") and order.index("write b") < order.index("hint b"):
            yield order


@defect("B-A1")
@pytest.mark.parametrize("order", list(_orders()), ids=lambda o: ",".join(s.replace(" ", "-") for s in o))
def test_and_of_two_assets_wakes_once_after_both_in_every_order(world, order):
    consumer = world.deploy(waits_for("consumer_p", [A, B]))
    members = {"a": A, "b": B}
    run, written = None, set()
    for step in order:
        if step == "register":
            run = world.start_run(consumer, DATE)
        elif step.startswith("write"):
            world.write_event(members[step[-1]], DATE)
            written.add(step[-1])
        else:
            world.deliver_hints(members[step[-1]])
        if run is not None and written != {"a", "b"}:
            assert world.running_tasks(run) == [], f"woke after {step!r} with only {sorted(written)} written"
    assert world.running_tasks(run) == ["consumer"]
    assert len(world.signals(run, "consumer")) == 1


@pytest.mark.parametrize("member", [A, B], ids=["a", "b"])
def test_or_wakes_on_either_member(world, member):
    run = world.start_run(world.deploy(waits_for("p", [A | B])), DATE)
    world.write_event(member, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


@defect("B-A2")
@pytest.mark.parametrize("first", [B, C], ids=["b", "c"])
def test_mixed_and_or_needs_a_and_one_of_b_or_c(world, first):
    run = world.start_run(world.deploy(waits_for("p", [A, B | C])), DATE)
    world.write_event(first, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


@defect("B-A3")
def test_an_early_and_member_is_rechecked_for_freshness(world):
    run = world.start_run(world.deploy(waits_for("p", [A.within(hours=2), B])), DATE)
    world.write_event(A, DATE)
    world.deliver_hints()
    world.clock.advance(hours=3)            # a's fact is now older than two hours
    world.write_event(B, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


@defect("B-C1")
def test_a_backfill_does_not_pass_on_todays_fact(world):
    consumer = world.deploy(waits_for("p", [A]))
    world.write_event(A, DATE)
    world.deliver_hints()
    run = world.start_run(consumer, "2026-10-13")
    assert world.running_tasks(run) == []
    world.write_event(A, "2026-10-13")
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


def test_a_fact_for_a_later_partition_does_not_satisfy_an_earlier_run(world):
    world.write_event(A, "2026-11-13")
    world.deliver_hints()
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    assert world.running_tasks(run) == []


def test_a_weekly_fact_from_monday_satisfies_the_rest_of_its_week(world):
    world.write_event(WEEKLY, "2026-W46")    # Monday 2026-11-09
    world.deliver_hints()
    run = world.start_run(world.deploy(waits_for("p", [WEEKLY])), "2026-11-10")
    assert world.running_tasks(run) == ["consumer"]


@defect("B-K1")
def test_same_named_waiting_tasks_in_two_pipelines_both_wake(world):
    first = world.start_run(world.deploy(waits_for("p1", [A])), DATE)
    second = world.start_run(world.deploy(waits_for("p2", [A])), DATE)
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(first) == ["consumer"]
    assert world.running_tasks(second) == ["consumer"]


@defect("B-T1")
def test_a_repeated_hint_does_not_disturb_a_running_task(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    # The pointer outlives the wake (its delete fails, which is harmless), so the next hint still arrives.
    world.inject_failure("DeleteItem", "DependencySubscriptionsTable", "InternalServerError")
    world.write_event(A, DATE)
    world.deliver_hints()
    world.write_event(A, DATE)              # a second fact for the same partition
    world.deliver_hints()
    assert world.status(run, "consumer") == "running"
    assert len(world.signals(run, "consumer")) == 1
    assert world.warnings() == []           # a repeated hint is a no-op, not an anomaly


def test_a_re_registered_task_is_woken_exactly_once(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.time_out_registration(run, "consumer")     # the wrapper retries and registers again
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]
    assert len(world.signals(run, "consumer")) == 1


# ── writers ─────────────────────────────────────────────────────────────


@defect("B-M1")
def test_mark_success_writes_the_fact_for_the_marked_runs_partition(world):
    produced = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [A]})])), "2026-11-01")
    world.mark_success(produced, "build")            # done on 2026-11-12
    world.deliver_hints()
    consumer = world.start_run(world.deploy(waits_for("c", [A])), "2026-11-01")
    assert world.running_tasks(consumer) == ["consumer"]


@defect("B-F1")
def test_a_failed_fan_out_does_not_fail_the_producer(world):
    producer = world.deploy(pipeline("producer", [("build", {"outlets": [A]})]))
    world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    world.start_run(world.deploy(waits_for("c", [A])), DATE)
    for table in ("DependencySubscriptionsTable", "AssetSubscriptionsTable"):
        world.inject_failure("Query", table, "AccessDeniedException")
    run = world.start_run(producer, DATE)
    world.complete(run, "build")
    world.deliver_hints()
    assert world.status(run, "build") == "success"
    assert world.pipeline_status(run) == "SUCCEEDED"         # the producer's run, not just its row


# ── push ────────────────────────────────────────────────────────────────


@defect("B-U1")
def test_push_and_starts_exactly_one_run_for_the_day(world):
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A, B]))
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.push_runs(consumer) == []
    world.write_event(B, DATE)
    world.deliver_hints()
    world.write_event(B, DATE)              # a repeated event
    world.deliver_hints()
    assert len(world.push_runs(consumer)) == 1


@defect("B-X1")
def test_two_push_consumers_with_the_same_task_id_on_one_day_both_run(world):
    first = world.deploy(pipeline("pushed_1", [("report", {})], schedule=[A]))
    second = world.deploy(pipeline("pushed_2", [("report", {})], schedule=[A]))
    world.write_event(A, DATE)
    world.deliver_hints()
    runs = [run for consumer in (first, second) for run in world.push_runs(consumer)]
    assert len(runs) == 2
    for run in runs:
        assert world.pipeline_status(run) == "RUNNING"
        assert world.running_tasks(run) == ["report"]
    # Each task is its own row: colliding names would make both runs share one.
    assert len({world.row(run, "report")["pipeline_execution"] for run in runs}) == 2


# ── loud failures ───────────────────────────────────────────────────────


@defect("B-G1")
def test_a_failed_registration_fails_the_task_with_its_cause(world):
    world.inject_failure("PutItem", "PipelineTokensTable", "AccessDeniedException",
                         when=lambda params: params.get("Item", {}).get("status") == {"S": "waiting"})
    run = world.start_run(world.deploy(chain("p", "extract")), DATE)
    assert world.status(run, "extract") == "failed"
    assert "AccessDeniedException" in world.row(run, "extract")["error"]


@defect("B-S1")
def test_a_failed_auto_skip_write_does_not_wake_dependants(world):
    world.inject_failure("PutItem", "PipelineTokensTable", "AccessDeniedException",
                         when=lambda params: params.get("Item", {}).get("status") == {"S": "skipped"})
    run = world.start_run(world.deploy(chain("p", "extract", "load")), DATE, skip_tasks=["extract"])
    # The skip could not be recorded, so the task fails visibly instead of vanishing.
    assert world.status(run, "extract") == "failed"
    assert "AccessDeniedException" in world.row(run, "extract")["error"]
    assert world.running_tasks(run) == []
    assert world.status(run, "load") != "running"


# ── pause and restart ───────────────────────────────────────────────────


@defect("B-P1")
def test_an_asset_arriving_during_a_pause_wakes_the_task_after_resume(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.pause(run)
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == []
    world.resume(run)
    assert world.running_tasks(run) == ["consumer"]


@defect("B-P2")
def test_resume_does_not_run_a_task_whose_assets_are_missing(world):
    run = world.start_run(world.deploy(waits_for("p", [A], deps=["extract"])), DATE)
    world.pause(run)
    world.complete(run, "extract")
    world.resume(run)
    assert world.running_tasks(run) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


@defect("B-P3")
def test_a_task_that_saves_its_pause_token_while_resume_runs_is_released_by_the_sweeper(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.hold("RunTaskHelperSfn")
    world.complete(run, "extract")          # load is signalled; its run_task waits to start
    world.pause(run)
    world.hold("PauseWaiterSfn")
    world.release("RunTaskHelperSfn")       # it reads the pause and enters its pause wait
    world.resume(run)                       # before its token is saved, so resume cannot release it
    world.release("PauseWaiterSfn")
    assert world.running_tasks(run) == []  # held until its 12-hour pause heartbeat
    world.clock.advance(minutes=11)
    world.sweep()
    assert world.running_tasks(run) == ["load"]
    assert any(w["kind"] == "sweeper" and w["task_name"] == "load" for w in world.warnings())


@defect("B-P4")
def test_a_task_whose_pause_token_cannot_be_saved_fails_with_the_cause(world):
    world.inject_failure("UpdateItem", "PipelineTokensTable", "AccessDeniedException",
                         when=lambda params: "pause_token = :token" in params.get("UpdateExpression", ""))
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.hold("RunTaskHelperSfn")
    world.complete(run, "extract")
    world.pause(run)
    world.release("RunTaskHelperSfn")       # it reads the pause; pause_waiter cannot save its token
    assert world.status(run, "load") == "failed"
    assert "could not enter its pause wait" in world.row(run, "load")["error"]
    assert world.running_tasks(run) == []


def test_a_task_resume_could_not_check_is_woken_by_the_sweeper_without_a_miss(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.pause(run)
    world.complete(run, "extract")          # load is parked: its run is paused
    world.clock.advance(minutes=30)
    world.fail_signals("ThrottlingException", times=1)
    world.resume(run)                       # the reconciler's signal to load is throttled: deferred
    assert world.running_tasks(run) == []
    world.clock.advance(minutes=6)
    world.sweep()
    assert world.running_tasks(run) == ["load"]
    assert [w for w in world.warnings() if w["kind"] == "sweeper"] == []   # resume deferred it: no lost hint


@defect("B-R1")
def test_a_restarted_task_is_still_gated_on_its_assets(world):
    run = world.start_run(world.deploy(waits_for("p", [A.within(hours=1)])), DATE)
    world.write_event(A, DATE)
    world.deliver_hints()
    world.complete(run, "consumer", status="failed")
    world.clock.advance(hours=3)
    world.restart(run, "consumer")
    assert world.running_tasks(run) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


# ── the sweeper and the bell ────────────────────────────────────────────


def test_a_lost_hint_is_recovered_by_the_sweeper_with_a_warning(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.write_event(A, DATE)              # its hint is never delivered
    world.clock.advance(minutes=40)
    world.sweep()
    assert world.running_tasks(run) == ["consumer"]
    assert any(w["kind"] == "sweeper" for w in world.warnings())


def test_a_lost_notify_after_mark_success_warns_and_the_sweeper_wakes_the_dependant(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.fail_starts("NotifyDependentsSfn", "ThrottlingException", times=1)
    world.mark_success(run, "extract")
    assert world.running_tasks(run) == []
    [warning] = world.warnings()
    assert (warning["kind"], warning["task_name"], warning["task_date"]) == ("notify", "extract", DATE)
    world.clock.advance(minutes=40)
    world.sweep()
    assert world.running_tasks(run) == ["load"]


def test_a_wrapper_stopped_outside_polyris_is_aborted_with_an_explanation(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.stop_wrapper_externally(run, "consumer")
    world.clock.advance(hours=74)           # past its liveness deadline: 3 x 24 h + 1 h (ADR #130 §3)
    world.sweep()
    row = world.row(run, "consumer")
    assert row["status"] == "aborted"
    assert "can no longer be woken" in row["error"]


def test_a_push_start_that_failed_transiently_is_retried_by_the_sweeper(world):
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    world.fail_starts(consumer, "ThrottlingException", times=1)
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.push_runs(consumer) == []
    world.clock.advance(minutes=6)
    world.sweep()
    assert len(world.push_runs(consumer)) == 1


def test_a_push_whose_stream_record_was_lost_is_started_by_the_catch_up(world):
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    world.write_event(A, DATE)              # its stream record is never delivered
    world.sweep()
    assert len(world.push_runs(consumer)) == 1
    world.deliver_hints()                   # delivered late: the run name makes it a no-op
    assert len(world.push_runs(consumer)) == 1


@defect("B-N1")
def test_a_warning_about_an_old_partition_reaches_the_bell(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), "2026-10-13")
    world.stop_wrapper_externally(run, "consumer")
    world.clock.advance(days=4)
    world.sweep()
    assert any(w["kind"] == "zombie" and w["task_date"] == "2026-10-13" for w in world.warnings())


# ── the matrix rows of COORDINATION_DESIGN §4.2 not pinned above ─────────


@defect("B-O1")
def test_an_operator_skip_holds_when_the_wrapper_could_not_be_stopped(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.fail_stops("ThrottlingException", times=1)
    world.skip(run, "load")                 # the console cannot stop load's wrapper, which goes on waiting
    world.time_out_registration(run, "load")    # orchestration_timeout later it registers again
    world.complete(run, "extract")
    assert world.status(run, "load") == "skipped"
    assert world.running_tasks(run) == []
    assert world.wrapper(run, "load").status == "SUCCEEDED"   # it ended as superseded


def test_an_operator_skip_while_the_task_waits_ends_its_wrapper_without_running_it(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.skip(run, "load")                 # while load still waits for extract
    assert world.wrapper(run, "load").status == "ABORTED"   # the console stops it
    world.complete(run, "extract")
    assert world.status(run, "load") == "skipped"
    assert world.running_tasks(run) == []


@pytest.mark.parametrize("rule,outcome,expected", [
    ("all_success", "failed", "upstream_failed"),
    ("all_success", "skipped", None),           # a manual skip counts as done: load runs
    ("one_success", "failed", "upstream_failed"),
    ("all_done", "failed", None),
    ("all_skipped", "failed", "skipped"),
    ("none_skipped", "skipped", "skipped"),
])
def test_each_trigger_rule_after_an_upstream_failure_or_skip(world, rule, outcome, expected):
    run = world.start_run(world.deploy(pipeline("orders", [("extract", {}), ("load", {"trigger_rule": rule})],
                                                [("extract", "load")])), DATE)
    if outcome == "failed":
        world.complete(run, "extract", status="failed")
    else:
        world.skip(run, "extract")
    if expected is None:
        assert world.running_tasks(run) == ["load"]
    else:
        assert world.status(run, "load") == expected and world.running_tasks(run) == []


@pytest.mark.parametrize("asset_first", [True, False], ids=["asset-first", "dependency-first"])
def test_a_task_with_a_dependency_and_an_asset_waits_for_both_in_either_order(world, asset_first):
    run = world.start_run(world.deploy(waits_for("p", [A], deps=["extract"])), DATE)
    if asset_first:
        world.write_event(A, DATE)
        world.deliver_hints()
        assert world.running_tasks(run) == ["extract"]
        world.complete(run, "extract")
    else:
        world.complete(run, "extract")
        assert world.running_tasks(run) == []
        world.write_event(A, DATE)
        world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


def test_two_runs_of_one_task_wake_independently(world):
    orders = world.deploy(chain("orders", "extract", "load"))
    first, second = world.start_run(orders, DATE), world.start_run(orders, "2026-11-13")
    world.complete(second, "extract")
    assert (world.running_tasks(first), world.running_tasks(second)) == (["extract"], ["load"])
    world.complete(first, "extract")
    assert world.running_tasks(first) == ["load"]


def test_a_registration_that_cannot_reach_the_reconciler_leaves_the_task_to_the_sweeper(world):
    world.write_event(A, DATE)              # the asset is there before the run starts
    world.deliver_hints()
    world.fail_invokes("ReconcileFunction", times=20)       # every retry of registration's call fails
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    assert (world.status(run, "consumer"), world.running_tasks(run)) == ("waiting", [])
    world.sweep()
    assert world.running_tasks(run) == ["consumer"]


def test_a_daily_and_weekly_schedule_runs_daily_with_the_latest_weekly_fact(world):
    consumer = world.deploy(pipeline("report", [("report", {})], schedule=[A, WEEKLY]))
    world.write_event(WEEKLY, "2026-W46")    # Monday 2026-11-09
    world.deliver_hints()
    assert world.push_runs(consumer) == []  # no daily fact yet
    for day in ("2026-11-10", "2026-11-11"):
        world.write_event(A, day)
        world.deliver_hints()
    world.deliver_hints()                   # a repeated delivery starts nothing more
    assert [run.date for run in world.push_runs(consumer)] == ["2026-11-10", "2026-11-11"]
