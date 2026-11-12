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
@needs_binding("operations")
def test_mark_success_writes_the_fact_for_the_marked_runs_partition(world):
    produced = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [A]})])), "2026-11-01")
    world.mark_success(produced, "build")            # done on 2026-11-12
    world.deliver_hints()
    consumer = world.start_run(world.deploy(waits_for("c", [A])), "2026-11-01")
    assert world.running_tasks(consumer) == ["consumer"]


@defect("B-F1")
@needs_binding("push")
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


# ── push ────────────────────────────────────────────────────────────────


@defect("B-U1")
@needs_binding("push")
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
@needs_binding("push")
def test_two_push_consumers_with_the_same_task_id_on_one_day_both_run(world):
    first = world.deploy(pipeline("pushed_1", [("report", {})], schedule=[A]))
    second = world.deploy(pipeline("pushed_2", [("report", {})], schedule=[A]))
    world.write_event(A, DATE)
    world.deliver_hints()
    for consumer in (first, second):
        [run] = world.push_runs(consumer)
        assert world.running_tasks(run) == ["report"]


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
@needs_binding("operations")
def test_an_asset_arriving_during_a_pause_wakes_the_task_after_resume(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.pause(run)
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == []
    world.resume(run)
    assert world.running_tasks(run) == ["consumer"]


@defect("B-P2")
@needs_binding("operations")
def test_resume_does_not_run_a_task_whose_assets_are_missing(world):
    run = world.start_run(world.deploy(waits_for("p", [A], deps=["extract"])), DATE)
    world.pause(run)
    world.complete(run, "extract")
    world.resume(run)
    assert world.running_tasks(run) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]


@defect("B-R1")
@needs_binding("operations")
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


@needs_binding("sweeper")
def test_a_lost_hint_is_recovered_by_the_sweeper_with_a_warning(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.write_event(A, DATE)              # its hint is never delivered
    world.clock.advance(minutes=40)
    world.sweep()
    assert world.running_tasks(run) == ["consumer"]
    assert any(w["kind"] == "sweeper" for w in world.warnings())


@needs_binding("sweeper")
def test_a_wrapper_stopped_outside_polyris_is_aborted_with_an_explanation(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.stop_wrapper_externally(run, "consumer")
    world.clock.advance(minutes=40)
    world.sweep()
    row = world.row(run, "consumer")
    assert row["status"] == "aborted"
    assert "can no longer be woken" in row["error"]


@defect("B-N1")
@needs_binding("sweeper")
def test_a_warning_about_an_old_partition_reaches_the_bell(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), "2026-10-13")
    world.stop_wrapper_externally(run, "consumer")
    world.clock.advance(days=2)
    world.sweep()
    assert any(w["kind"] == "zombie" and w["task_date"] == "2026-10-13" for w in world.warnings())
