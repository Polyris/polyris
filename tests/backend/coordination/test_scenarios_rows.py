"""Red scenarios for tasks as rows (ADR #137): one per class of bug the model must make impossible.

Each is written before the code that satisfies it and skipped until its stage
is bound (`needs_binding`, stages.py); its verbs raise NeedsBinding until then.
A scenario that finishes a task's work needs the new run_task (`rows-run-task`,
TR-5), and one with an operator's action the console's (`rows-ops`, TR-6).
From its stage on, the checks of row_invariants.py run under it too, so each
scenario asserts only what its class needs. The classes are in defects.py
(`R-…`); R-S1 to R-S4 are the four stability blockers of the plan's review.
Once a scenario runs, test_meta.py asks for a probe that brings its bug back.
"""

import json
from datetime import timedelta

import pytest

from dags import asset, chain, pipeline, waits_for
from polyris.constants import WarningKind
from world import JSON_NULL

DATE = "2026-11-18"
defect = pytest.mark.defect
needs_binding = pytest.mark.needs_binding


def warnings_of(world, kind):
    return [w for w in world.warnings() if w.get("kind") == kind]


def left_waiting(world, run):
    """The tasks of `run` whose rows still wait; none once every task was started or decided."""
    return [t.task_id for t in run.pipeline.dag.tasks if world.status(run, t.task_id) == "waiting"]


def a_pass_reads_the_run(call):
    """The dispatcher's read of a run's task rows (not of its spec version): where a scenario puts an action
    into a pass."""
    return (call.component == "lambda:reconcile" and call.operation == "BatchGetItem"
            and all(table.endswith("-pipeline-tokens") for table in call.tables))


@defect("R-L1")
def test_a_lost_hint_heals_at_its_run_rows_next_check_with_one_warning(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.lose_task_hint(run, "extract")           # the record of extract's success never reaches the mapping
    world.complete(run, "extract")
    world.deliver_task_hints()
    assert world.status(run, "load") == "waiting"
    world.clock.advance(minutes=30)
    world.sweep()
    assert world.running_tasks(run) == ["load"]
    assert len(warnings_of(world, WarningKind.SWEEPER)) == 1
    world.complete(run, "load")
    world.run_to_the_end()


@defect("R-S1")
def test_a_pass_out_of_budget_leaves_the_rest_to_the_next_pass_at_once(world):
    world.shrink_pass_budget(rows=50)
    run = world.start_run(world.deploy(pipeline("wide", [(f"t{i:03d}", {}) for i in range(200)])), DATE)
    world.deliver_task_hints()                     # each pass sets left_at, whose record wakes the next
    assert left_waiting(world, run) == []          # all 200 started, without waiting for the sweeper


@defect("R-S2")
def test_a_late_start_run_attempt_cannot_revive_a_run_the_engine_stopped(world):
    orders = world.deploy(chain("orders", "extract"))
    world.hold_start_run_retry(orders)             # attempt 1 writes the run as creating and dies
    run = world.start_run(orders, DATE)
    world.clock.advance(hours=1)                   # past Start_Run's retry window
    world.sweep()                                  # the engine stops the run that never became active
    world.release_start_run_retry(orders)          # the late attempt finds the stop mark
    assert world.running_tasks(run) == []
    assert world.pipeline_status(run) == "FAILED"
    assert warnings_of(world, WarningKind.COORDINATION)


@defect("R-S3")
def test_a_throttled_start_warns_after_15_minutes_and_fails_after_6_hours(world):
    world.fail_starts("RunTaskHelperSfn", "ThrottlingException", times=100_000)
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    assert world.status(run, "extract") == "deps_ready"
    world.clock.advance(minutes=14)
    world.sweep()
    assert warnings_of(world, WarningKind.COORDINATION) == []     # a short burst of throttling warns no one
    world.clock.advance(minutes=2)
    world.sweep()
    assert len(warnings_of(world, WarningKind.COORDINATION)) == 1
    world.clock.advance(hours=6)
    world.sweep()
    assert world.status(run, "extract") == "failed"


@defect("R-S3")
def test_a_denied_start_warns_at_once_and_counts_toward_no_limit(world):
    world.fail_starts("RunTaskHelperSfn", "AccessDeniedException", times=100_000)
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    assert len(warnings_of(world, WarningKind.COORDINATION)) == 1
    world.clock.advance(hours=7)
    world.sweep()
    assert world.status(run, "extract") == "deps_ready"           # it waits for the permission
    assert len(warnings_of(world, WarningKind.COORDINATION)) == 1


@needs_binding("rows-ops")
@defect("R-S4")
def test_a_restart_with_dependants_inside_a_passs_read_reruns_the_dependant(world):
    orders = pipeline("orders", [("extract", {}), ("load", {}), ("audit", {})], [("extract", "load")])
    run = world.start_run(world.deploy(orders), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract", status="failed")
    world.fail_task(run, "extract")                # the operator's decision makes the failure final
    world.deliver_task_hints()
    assert world.status(run, "load") == "upstream_failed"
    # The review's race: the pass that audit's success wakes reads extract before the Restart and load
    # after it, since a BatchGetItem that DynamoDB answers in two parts is no snapshot.
    world.boundary.unprocess(table=world.stack.tables["PipelineTokensTable"], keys=("execution_name",), keep=1)
    world.boundary.between(after_read=a_pass_reads_the_run,
                           then=lambda: world.restart_with_dependants(run, "extract"))
    world.complete(run, "audit")
    world.deliver_task_hints()
    assert world.status(run, "load") == "waiting"  # not decided again from the read of extract's failure
    world.complete(run, "extract")
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["load"]


@defect("R-D1")
def test_two_passes_at_once_start_each_task_once(world):
    run = world.start_run(world.deploy(pipeline("wide", [(f"t{i}", {}) for i in range(5)])), DATE)
    world.two_passes_at_once(run)
    world.deliver_task_hints()
    assert left_waiting(world, run) == []          # invariant 4 checks one run_task per row and generation


@needs_binding("rows-ops")
@defect("R-P1")
def test_a_pause_between_a_passs_read_and_its_write_starts_nothing(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.boundary.between(after_read=a_pass_reads_the_run, then=lambda: world.pause(run))
    world.deliver_task_hints()
    assert world.running_tasks(run) == []
    world.resume(run)
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["load"]


@needs_binding("rows-ops")
@defect("R-P1")
def test_a_stop_between_a_passs_read_and_its_write_starts_nothing(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.boundary.between(after_read=a_pass_reads_the_run, then=lambda: world.stop_run(run))
    world.deliver_task_hints()
    assert world.running_tasks(run) == []
    assert world.status(run, "load") == "stopped"


@defect("R-F1")
def test_a_partial_read_starts_nothing_until_the_read_completes(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load", "report")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.boundary.unprocess(table=world.stack.tables["PipelineTokensTable"], keys=("execution_name",), keep=1, times=3)
    world.deliver_task_hints()                     # invariant 10 judges each pass
    world.clock.advance(minutes=30)
    world.sweep()
    assert world.running_tasks(run) == ["load"]


@defect("R-E1")
def test_a_run_whose_answer_was_lost_is_answered_at_its_next_check(world):
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.fail_signals("ThrottlingException", times=1, call="send_task_success")
    world.deliver_task_hints()                     # the pass that finds every row final cannot answer
    assert world.pipeline_status(run) == "RUNNING"
    world.clock.advance(minutes=30)
    world.sweep()
    assert world.pipeline_status(run) == "SUCCEEDED"


@defect("R-T1")
def test_a_waiting_dependant_of_a_missing_upstream_row_fails_with_the_cause(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.delete_row(run, "extract")
    world.clock.advance(minutes=30)
    world.sweep()
    assert world.status(run, "load") == "failed"
    assert warnings_of(world, WarningKind.COORDINATION)


@defect("R-T1")
def test_a_task_row_without_its_run_row_is_stopped(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.delete_run_row(run)
    world.complete(run, "extract")
    world.deliver_task_hints()                     # the change of extract's row starts a pass, which reads no run row
    assert world.status(run, "load") == "stopped"


@defect("R-O1")
def test_a_succeeded_upstream_without_its_output_keeps_its_dependant_from_running(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.delete_output(run, "extract")            # gone, as when it expired before a late Restart
    world.plant(f"invariant 12: {run.execution_name('extract')} reads success, but its output is not recorded")
    world.deliver_task_hints()                     # load's run_task finds no output for extract (D109)
    assert world.running_tasks(run) == []
    assert world.status(run, "load") in ("waiting_decision", "failed")


@needs_binding("rows-ops")
@defect("R-O1")
def test_a_restart_of_a_run_that_started_a_year_ago_is_refused(world):
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.deliver_task_hints()
    world.clock.advance(days=364, hours=1)         # past a year less a day (RESTART_TTL_MARGIN)
    with pytest.raises(AssertionError, match=r"→ 4\d\d"):
        world.restart(run, "extract")             # refused, so no output the rerun reads has expired (D109)
    assert world.status(run, "extract") == "success"


@defect("R-O2")
def test_an_output_near_the_payload_limit_is_recorded_and_one_over_it_waits_for_a_decision(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract", output={"rows": "x" * 200_000})
    world.deliver_task_hints()
    assert world.status(run, "extract") == "success"
    assert world.running_tasks(run) == ["load"]
    world.complete(run, "load", output={"rows": "x" * 300_000})    # over 256 KB: claim-check territory
    world.deliver_task_hints()
    assert world.status(run, "load") == "waiting_decision"
    assert "States.DataLimitExceeded" in str(world.row(run, "load"))
    assert world.pipeline_status(run) == "RUNNING"


@needs_binding("rows-ops")
@defect("R-W1")
def test_a_limit_due_in_a_pause_fires_after_the_resume_moved_by_the_paused_time(world):
    timed = pipeline("orders", [("extract", {}), ("load", {"orchestration_timeout": timedelta(hours=1)})],
                     [("extract", "load")])
    run = world.start_run(world.deploy(timed), DATE)
    world.deliver_task_hints()
    world.pause(run)
    world.deliver_task_hints()
    world.clock.advance(hours=2)
    world.sweep()
    assert world.status(run, "load") != "failed"   # no limit fires in a pause
    world.resume(run)
    world.deliver_task_hints()
    world.clock.advance(minutes=59)                # the hour's limit, moved by the two paused hours
    world.sweep()
    assert world.status(run, "load") == "waiting"
    world.clock.advance(minutes=2)
    world.sweep()
    assert world.status(run, "load") == "failed"


@needs_binding("rows-migration")
@defect("R-U1")
def test_a_1_2_row_left_in_flight_across_the_upgrade_is_stopped_with_the_cause(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.upgrade_platform()
    world.clock.advance(minutes=30)
    world.sweep()
    assert world.status(run, "load") == "stopped"
    assert warnings_of(world, WarningKind.COORDINATION)


@defect("R-C1")
def test_a_cancelled_success_is_tried_again_and_recorded_with_its_fact(world):
    """ADR #137 §1: after TransactionCanceledException run_task reads its row; still running under its generation,
    it sends the transaction again, with a new time for the fact, and the consumer of the fact starts."""
    orders = asset("ns/orders")
    consumer = world.start_run(world.deploy(waits_for("consumer_p", [orders])), DATE)
    run = world.start_run(world.deploy(pipeline("producer", [("build", {"outlets": [orders]})])), DATE)
    world.deliver_task_hints()
    world.inject_failure("TransactWriteItems", "AssetEventsTable", "TransactionCanceledException", times=1)
    world.complete(run, "build")
    world.deliver_task_hints()
    assert world.status(run, "build") == "success"
    world.deliver_hints(orders)
    world.deliver_task_hints()
    assert world.running_tasks(consumer) == ["consumer"]


@defect("R-N2")
def test_a_task_after_a_final_failure_gets_its_status_and_never_an_older_runs_output(world):
    """The date's output# still holds what the first run of the date wrote; the second run's all_done dependant of
    the failed upstream gets that upstream's status alone (ADR #137 §1, ADR #138 §2)."""
    orders = world.deploy(pipeline("orders", [("extract", {}), ("report", {"trigger_rule": "all_done"})],
                                   [("extract", "report")]))
    first = world.start_run(orders, DATE)
    world.deliver_task_hints()
    world.complete(first, "extract", output={"rows": 1})
    world.deliver_task_hints()
    world.complete(first, "report")
    world.deliver_task_hints()
    second = world.start_run(orders, DATE)
    world.deliver_task_hints()
    world.complete(second, "extract", status="failed")      # no retries: it waits for a decision
    world.clock.advance(hours=5, minutes=1)                 # nobody decides: it fails at the decision timeout
    world.sweep()
    world.deliver_task_hints()
    assert world.status(second, "extract") == "failed"
    assert world.work_input(second, "report")["upstream"] == {"extract": {"status": "failed", "skip_origin": None}}


@defect("R-J1")
@pytest.mark.parametrize("returned", ["ok", "", JSON_NULL, 42, ["a", 1], {"rows": 1}],
                         ids=["string", "empty-string", "null", "number", "list", "object"])
def test_a_dependant_reads_whatever_its_upstream_returned_a_string_included(world, returned):
    """The review of TR-5: a string result stored as it was ("ok") is no JSON, the dependant's read failed at every
    attempt, and it never ran. A result is stored as JSON, and reaches the dependant as it was returned."""
    value = None if returned is JSON_NULL else returned
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract", output=returned)
    world.deliver_task_hints()
    assert json.loads(world.stores(run, "extract")["output"]["result"]) == value
    assert world.running_tasks(run) == ["load"]
    assert world.work_input(run, "load")["upstream"]["extract"]["output"] == value


@defect("R-J1")
def test_a_string_result_a_1_2_run_task_stored_reaches_the_dependant_as_that_string(world):
    """1.2 stored a string result as it was; a run that skips the task (skip_tasks) reads that row of the date."""
    orders = world.deploy(chain("orders", "extract", "load"))
    world.write_output(orders, "extract", DATE, "ok")
    run = world.start_run(orders, DATE, skip_tasks=["extract"])
    world.deliver_task_hints()
    assert world.running_tasks(run) == ["load"]
    assert world.work_input(run, "load")["upstream"]["extract"]["output"] == "ok"


@defect("R-N3")
def test_a_task_beside_an_upstream_that_still_runs_never_reads_its_older_output(world):
    """one_success starts its task at the first success while the other upstream still runs, and that upstream's
    output# of the date holds what an earlier run wrote: xcom refuses it, naming the status (the review of TR-5)."""
    from polyris import xcom

    orders = world.deploy(pipeline("orders", [("fast", {}), ("slow", {}), ("pick", {"trigger_rule": "one_success"})],
                                   [("fast", "pick"), ("slow", "pick")]))
    first = world.start_run(orders, DATE)
    world.deliver_task_hints()
    world.complete(first, "fast")
    world.complete(first, "slow", output={"rows": "of the first run"})
    world.deliver_task_hints()
    world.complete(first, "pick")
    world.deliver_task_hints()
    second = world.start_run(orders, DATE)
    world.deliver_task_hints()
    world.complete(second, "fast")
    world.deliver_task_hints()
    assert sorted(world.running_tasks(second)) == ["pick", "slow"]
    event = world.work_input(second, "pick")
    assert event["upstream"]["slow"] == {"status": "running", "skip_origin": None}
    with pytest.raises(xcom.XComMissingError, match="status: running"):
        world.xcom_get(second, "pick", "slow")
    assert world.xcom_get(second, "pick", "slow", raise_on_missing=False) is None


@defect("R-T2")
def test_a_timed_out_attempt_goes_through_the_tasks_retries(world):
    retried = {"retries": 1, "retry_delay": timedelta(seconds=10), "execution_timeout": timedelta(minutes=30)}
    run = world.start_run(world.deploy(pipeline("orders", [("extract", retried)])), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract", status="timed_out")      # the attempt reaches its execution_timeout
    assert world.running_tasks(run) == ["extract"]          # its retry runs, at least a minute later on AWS
    assert world.row(run, "extract")["retry_attempt"] == 1
    world.complete(run, "extract")
    world.deliver_task_hints()
    assert world.status(run, "extract") == "success"


def test_each_row_a_success_writes_keeps_its_lifetime(world):
    """ADR #137 §5, D108: the task row a year and 90 days from its latest write, the date's output a year, the
    task's input 90 days; nothing shortens them at the run's end."""
    from polyris.coordination import retention
    from polyris.coordination.times import parse_utc

    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract", output={"rows": 1})
    world.deliver_task_hints()
    assert world.pipeline_status(run) == "SUCCEEDED"
    written = parse_utc(str(world.row(run, "extract")["finished_at"]))      # the success's transaction
    stores = world.stores(run, "extract")
    for row, lifetime in ((world.row(run, "extract"), retention.HISTORY), (stores["output"], retention.OUTPUT),
                          (stores["input"], retention.INPUT)):
        assert abs(int(row["ttl"]) - retention.ttl(written, lifetime)) < 60, (row["execution_name"], lifetime)


def test_a_task_costs_few_state_transitions(world):
    """What a task's run_task takes, measured here (TR-5): a success with one upstream and one outlet, and a root
    task's. 1.2 took a wrapper of its own besides; a regression in the template shows here first."""
    orders = asset("ns/orders")
    run = world.start_run(world.deploy(pipeline("orders", [("extract", {}), ("load", {"outlets": [orders]})],
                                                [("extract", "load")])), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.deliver_task_hints()
    world.complete(run, "load")
    world.deliver_task_hints()
    assert world.run_task_transitions(run, "extract") == [13]          # $0.000325 each at $0.025 per 1,000
    assert world.run_task_transitions(run, "load") == [14]             # and one Map iteration per upstream read


def test_a_tasks_timeline_shows_the_decision_then_its_run_task(world):
    """The dispatcher writes its decision, run_task its start and its end, all under the row's run_task name: one
    timeline per generation (ADR #137 §1, invariant 4)."""
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.deliver_task_hints()
    world.complete(run, "extract")
    world.deliver_task_hints()
    events = world.timeline(run, "extract")
    assert [e["event_type"] for e in events] == ["DEPS_READY", "TASK_STARTED", "TASK_FINISHED"]
    assert {e["task_run_id"] for e in events} == {world.row(run, "extract")["task_run_id"]}
