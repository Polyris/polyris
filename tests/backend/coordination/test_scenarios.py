"""Coordination scenarios (ADR #130, Testing).

Each scenario checks both sides of a wake: the task starts once its
conditions hold, and never before. They speak only the World API; a
scenario whose code path has not landed yet is marked needs_binding(stage)
and skipped (stages.py). Defect ids refer to defects.py.

These scenarios ran on the engine of ADR #130 until TR-4 switched the
generator to Start_Run (D115). They run on the engine of ADR #137 since TR-5
bound the new run_task; the ones that wait for the operator actions
(`rows-ops`, TR-6) or the upgrade (`rows-migration`, TR-8) say so. A scenario
that tested machinery 1.3 removed (registration, the wrapper and its signals,
notify_dependents) went with it. The tasks table's stream runs on its own
here, as in AWS; test_scenarios_rows.py delivers it by hand to order passes.
"""

import itertools

import pytest
from dags import asset, chain, pipeline, waits_for

DATE = "2026-11-12"
defect = pytest.mark.defect
needs_binding = pytest.mark.needs_binding

A, B, C = asset("ns/a"), asset("ns/b"), asset("ns/c")
WEEKLY = asset("ns/weekly", granularity="weekly")


@pytest.fixture(autouse=True)
def _tasks_stream_runs(world):
    world.tasks_stream_runs = True


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
    assert world.task_runs(run, "load") == 1


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
    assert world.task_runs(run, "consumer") == 1


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
    assert world.task_runs(run, "consumer") == 1
    assert world.warnings() == []           # a repeated hint is a no-op, not an anomaly


@defect("B-Q1")
def test_a_dependency_wait_that_times_out_fails_the_task_once(world):
    """D41: a wait for facts ends at its limit, 7 days by default (ADR #137 §5): the task fails once, with the
    cause, and its run ends; it stayed RUNNING in 1.2 when the answer was skipped (F16, live)."""
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.clock.advance(days=7, minutes=1)
    world.sweep()
    assert world.status(run, "consumer") == "failed"
    assert "waited past its limit" in str(world.row(run, "consumer")["error"])
    assert world.pipeline_status(run) == "FAILED"
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.running_tasks(run) == []


# ── writers ─────────────────────────────────────────────────────────────


@defect("B-M1")
def test_mark_success_writes_the_fact_for_the_marked_runs_partition(world):
    produced = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [A]})])), "2026-11-01")
    world.mark_success(produced, "build")            # done on 2026-11-12
    world.deliver_hints()
    consumer = world.start_run(world.deploy(waits_for("c", [A])), "2026-11-01")
    assert world.running_tasks(consumer) == ["consumer"]


@defect("B-M2")
def test_a_mark_success_whose_fact_cannot_be_recorded_records_nothing_and_a_restart_records_it(world):
    """1.3: the facts are part of Mark Successful's transaction (invariant 12), so it applies whole or not at all,
    and the console says it failed instead of reporting success."""
    produced = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [A]})])), DATE)
    world.deliver_task_hints()
    world.complete(produced, "build", status="failed")
    world.inject_failure("TransactWriteItems", "AssetEventsTable", "AccessDeniedException", times=1)
    with pytest.raises(AssertionError, match=r"task-success → 5\d\d"):
        world.mark_success(produced, "build")
    assert world.status(produced, "build") == "waiting_decision"
    consumer = world.start_run(world.deploy(waits_for("c", [A])), DATE)
    assert world.status(consumer, "consumer") == "waiting"
    world.clock.advance(seconds=1)
    world.restart(produced, "build")
    world.deliver_task_hints()
    world.complete(produced, "build")
    world.deliver_hints()
    assert world.running_tasks(consumer) == ["consumer"]


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


@defect("B-U2")
def test_a_consumer_deployed_after_the_facts_does_not_start_for_them(world):
    world.write_event(A, "2026-11-10")
    world.write_event(A, "2026-11-11")
    world.deliver_hints()                   # no consumer yet: these facts start nothing
    world.clock.advance(minutes=5)
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    world.clock.advance(hours=1)
    world.sweep()                           # the hourly catch-up replays the facts of the last 48 hours
    assert world.push_runs(consumer) == []
    world.write_event(A, DATE)
    world.deliver_hints()
    assert [run.date for run in world.push_runs(consumer)] == [DATE]


@defect("B-D1")
def test_a_scheduled_run_carries_the_day_it_was_scheduled_for(world):
    nightly = world.deploy(pipeline("nightly", [("load", {}), ("report", {})], [("load", "report")],
                                    schedule="cron(59 23 * * ? *)", variables={"region": "eu"}))
    world.clock.jump_to("2026-11-13T00:01:00.000Z")         # Scheduler started it two minutes late
    run = world.fire_schedule(nightly, scheduled_time=f"{DATE}T23:59:00Z")
    load = world.row(run, "load")
    assert load is not None and load["date"] == DATE
    assert load["triggered_by"] == "schedule"
    world.complete(run, "load")
    assert world.running_tasks(run) == ["report"]


@defect("B-D1")
def test_a_pushed_run_of_a_pipeline_with_variables_carries_the_facts_partition(world):
    consumer = world.deploy(pipeline("pushed_v", [("report", {})], schedule=[A], variables={"region": "eu"}))
    world.clock.jump_to("2026-11-14T00:30:00.000Z")         # the fact for the 12th is recorded on the 14th
    world.write_event(A, DATE)
    world.deliver_hints()
    [run] = world.push_runs(consumer)
    report = world.row(run, "report")
    assert report is not None and report["date"] == DATE


@defect("B-R2")
def test_a_restart_never_lets_a_dependant_run_on_the_old_status(world):
    run = world.start_run(world.deploy(chain("orders", "a", "b")), DATE)
    world.deliver_task_hints()
    world.complete(run, "a")
    world.deliver_task_hints()
    world.complete(run, "b")
    world.deliver_task_hints()
    world.restart(run, "a")
    world.restart(run, "b")                       # b's upstream waits again: b waits for it
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["a"] and world.status(run, "b") == "waiting"
    world.complete(run, "a")
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["b"]


@defect("B-D1")
def test_every_task_of_a_run_crossing_midnight_gets_the_day_it_started(world):
    plain = world.deploy(pipeline("plain", [("a", {}), ("b", {}), ("c", {})]))
    world.clock.jump_to(f"{DATE}T23:59:59.998Z")   # the execution starts before midnight, its first states after
    run = world.start_bare(plain)
    assert run.date == DATE
    assert [(world.row(run, t) or {}).get("date") for t in ("a", "b", "c")] == [DATE] * 3


@defect("B-U3")
def test_an_asset_dropped_from_the_schedule_no_longer_starts_the_pipeline(world):
    world.deploy(pipeline("pushed", [("report", {})], schedule=[A & B]))
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[B]))     # redeployed without A
    assert world.push_subscribers(A) == ["pushed"]          # registration does not remove the old subscription
    world.write_event(B, DATE)
    world.write_event(A, DATE)
    world.deliver_hints()
    assert len(world.push_runs(consumer)) == 1              # B's fact; A's starts nothing
    assert world.push_subscribers(A) == [] and world.push_subscribers(B) == ["pushed"]
    assert world.warnings() == []


@defect("B-U3")
def test_a_pipeline_moved_to_a_cron_loses_its_push_subscription_quietly(world):
    world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    consumer = world.deploy(pipeline("pushed", [("report", {})]))                  # now on a cron
    world.write_event(A, DATE)
    world.deliver_hints()
    assert world.push_runs(consumer) == []
    assert world.push_subscribers(A) == []
    assert world.warnings() == []


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


@defect("B-U2")
def test_a_consumer_registered_again_keeps_its_subscription_time(world):
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    world.write_event(A, DATE)              # its stream record is never delivered
    world.clock.advance(minutes=5)
    world.start_run(consumer, "2026-11-11")  # every run registers its subscriptions again
    world.clock.advance(hours=1)
    world.sweep()                           # the catch-up still sees a fact recorded after the subscription
    assert [run.date for run in world.push_runs(consumer)] == [DATE]


@defect("B-F2")
def test_an_unreadable_fact_fails_no_waiting_task(world):
    run = world.start_run(world.deploy(waits_for("p", [A])), DATE)
    world.write_event(A, "latest", writer="older version")   # a run date that is no partition key: unreadable
    world.deliver_hints()
    assert world.status(run, "consumer") == "waiting"
    world.write_event(A, DATE)              # the reader now meets both rows
    world.deliver_hints()
    assert world.running_tasks(run) == ["consumer"]
    assert any(w["kind"] == "coordination" and "cannot be read" in w["error"] for w in world.warnings())


@defect("B-W1")
def test_a_claim_whose_answer_is_lost_still_runs_the_task(world):
    orders = world.deploy(chain("orders", "extract"))
    world.inject_failure("UpdateItem", "PipelineTokensTable", "InternalServerError", times=1, after=True,
                         when=lambda params: str(params.get("ConditionExpression", "")).startswith("#s = :deps_ready"))
    run = world.start_run(orders, DATE)     # extract's claim is written, then its answer fails: run_task retries
    assert world.running_tasks(run) == ["extract"]


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


def test_a_lost_hint_after_mark_success_heals_at_the_runs_next_check(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.lose_task_hint(run, "extract")          # the record of the Mark Successful never reaches the mapping
    world.mark_success(run, "extract")
    world.deliver_task_hints()
    assert world.running_tasks(run) == []
    world.clock.advance(minutes=31)
    world.sweep()
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["load"]


def test_a_run_task_stopped_outside_polyris_fails_its_task_with_an_explanation(world):
    """The dispatcher judges the execution at the row's liveness check (ADR #137 §1): it ended without an
    outcome, so the task fails with how it ended, and the run ends; it stayed RUNNING in 1.2 (Z1, live)."""
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.stop_run_task_externally(run, "extract")
    world.clock.advance(minutes=11)         # past the row's first liveness check, 10 minutes after its start
    world.sweep()
    row = world.row(run, "extract")
    assert row["status"] == "failed"
    assert "ended ABORTED (Console: stopped by hand) without recording an outcome" in row["error"]
    assert world.pipeline_status(run) == "FAILED"


@defect("B-F3")
def test_a_success_whose_fact_cannot_be_recorded_waits_for_a_decision(world):
    """The status, the output and the facts are one transaction (ADR #137 §1, invariant 12): when a fact cannot
    be written, nothing of the success is, and the task waits for a decision with the cause. 1.2 wrote the row
    success and its fact apart, and the consumer never started (external review, 1.2.0)."""
    world.inject_failure("TransactWriteItems", "AssetEventsTable", "AccessDeniedException")
    producer = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [A]})])), DATE)
    consumer = world.start_run(world.deploy(waits_for("consumer-p", [A])), DATE)
    world.complete(producer, "build")
    row = world.row(producer, "build")
    assert row["status"] == "waiting_decision"
    assert "its success could not be recorded" in row["error"] and "AccessDeniedException" in row["error"]
    assert world.status(consumer, "consumer") == "waiting"


@defect("B-U4")
def test_the_catch_up_starts_no_run_that_a_cascade_none_backfill_completed(world):
    """A arrives without B: no run. B then comes from a cascade='none' backfill, which starts
    no consumer (ADR #57). The hourly catch-up replays A as of A's own time, when B did not
    exist yet; counting the later B started exactly the run ADR #57 forbids (external review, 1.2.0)."""
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A, B]))
    world.write_event(A, DATE)
    world.deliver_hints()
    backfill = world.start_run(world.deploy(pipeline("backfill_b", [("build", {"outlets": [B]})])), DATE,
                               _suppress_asset_event=True)
    world.complete(backfill, "build")
    world.deliver_hints()
    assert world.push_runs(consumer) == []
    world.clock.advance(hours=2)
    world.sweep()                               # the hourly catch-up replays the facts of 48 hours
    assert world.push_runs(consumer) == []


@defect("B-S2")
def test_stopping_a_run_ends_its_execution(world):
    """The console's Stop writes the run's stop mark; the engine stops the rows, the run's execution, and answers.
    The 1.2 route called a repository method that never existed and stopped nothing (external review, round 2)."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    assert world.stop_run(run)["stopping"] is True
    world.deliver_task_hints()
    assert world.pipeline_status(run) == "ABORTED"
    assert (world.status(run, "extract"), world.status(run, "load")) == ("stopped", "stopped")


@defect("B-RN4")
@pytest.mark.parametrize("action, status", [("skip", "skipped"), ("fail_task", "failed"), ("mark_success", "success"),
                                            ("stop_task", "stopped")])
def test_a_console_action_whose_timeline_cannot_be_written_still_applies(world, action, status):
    """1.3: the console answers no run; what follows its transaction (the timeline) is best effort, and a failure
    there must not answer 500 after the row has changed (architect review)."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)    # load waits for extract
    world.inject_failure("PutItem", "TaskEventsTable", "ProvisionedThroughputExceededException")
    getattr(world, action)(run, "load")
    assert world.status(run, "load") == status


@defect("B-M3")
def test_mark_successful_keeps_a_cascade_none_backfill_from_starting_consumers(world):
    """A cascade='none' backfill starts no consumer of its facts (ADR #57); marking one of its tasks
    successful recorded the facts with cascade='auto', which did (external review, 1.2.0)."""
    consumer = world.deploy(pipeline("pushed", [("report", {})], schedule=[A]))
    backfill = world.start_run(world.deploy(pipeline("backfill_a", [("build", {"outlets": [A]})])), DATE,
                               _suppress_asset_event=True)
    world.complete(backfill, "build", status="failed")
    world.mark_success(backfill, "build")
    world.deliver_hints()
    assert world.push_runs(consumer) == []


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
    """The bell finds a warning by when it was written, whatever the date of its run (records.warning_item)."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), "2026-10-13")
    world.lose_task_hint(run, "extract")           # the record of extract's success never reaches the dispatcher
    world.complete(run, "extract")
    world.clock.advance(minutes=30)
    world.sweep()                                  # its run row's check finds the lost wake-up, and says so
    assert any(w["kind"] == "sweeper" and w["task_date"] == "2026-10-13" for w in world.warnings())


# ── the matrix rows of COORDINATION_DESIGN §4.2 not pinned above ─────────


@defect("B-O1")
def test_an_operator_skip_holds_when_the_run_task_could_not_be_stopped(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.fail_stops("ThrottlingException", times=1)
    world.skip(run, "extract")                    # its run_task cannot be stopped and runs on
    world.complete(run, "extract")                # its late success is fenced out (invariant 5)
    world.deliver_task_hints()
    assert world.status(run, "extract") == "skipped"
    assert world.running_tasks(run) == ["load"]


@needs_binding("rows-migration")
@defect("B-O3")
def test_a_pre_upgrade_wrapper_never_runs_a_task_an_operator_decided(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load"), before_upgrade=True), DATE)
    world.fail_stops("ThrottlingException", times=10)
    world.skip(run, "load")                 # the console cannot stop load's v1.1.4 wrapper
    world.time_out_registration(run, "load")    # that wrapper retries its wait and registers again
    world.complete(run, "extract")
    assert world.status(run, "load") == "skipped"
    assert world.task_runs(run, "load") == 0


@defect("B-O4")
def test_run_task_never_starts_a_task_an_operator_decided_after_its_wait(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.hold("RunTaskHelperSfn")
    world.complete(run, "extract")          # load is claimed; its run_task waits to take a step
    world.fail_stops("ThrottlingException", times=10)
    world.skip(run, "load")                 # the console can stop neither its wrapper nor its run_task
    world.release("RunTaskHelperSfn")
    assert (world.status(run, "load"), world.running_tasks(run)) == ("skipped", [])
    assert world.task_runs(run, "load") == 1                # started, then superseded


@defect("B-O5")
def test_a_task_an_operator_decided_while_paused_is_never_released_to_run(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.pause(run)
    world.complete(run, "extract")
    world.deliver_task_hints()
    assert world.status(run, "load") == "waiting_paused"
    world.skip(run, "load")
    world.resume(run)
    world.deliver_task_hints()
    assert (world.status(run, "load"), world.running_tasks(run)) == ("skipped", [])


def test_an_operator_skip_while_the_task_waits_never_runs_it(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.skip(run, "load")                       # while load still waits for extract
    world.complete(run, "extract")
    world.deliver_task_hints()
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
    world.deliver_task_hints()
    if outcome == "failed":
        world.complete(run, "extract", status="failed")
        world.fail_task(run, "extract")           # the operator's decision makes the failure final (ADR #114)
    else:
        world.skip(run, "extract")
    world.deliver_task_hints()
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
