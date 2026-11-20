"""The stack's view for ADR #137's invariants (rows_view.py) sees what the checks read, and a violation
planted in the real stack is caught."""

from boundary import acting_as
from dags import chain
from row_invariants import CHECKS

DATE = "2026-11-18"


def _run(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.deliver_task_hints()
    return run


def test_the_view_sees_a_run_its_rows_and_the_run_task_it_started(world):
    run = _run(world)
    view = world.rows_view()
    [seen] = [r for r in view.runs() if r.id == run.name]
    assert (seen.status, seen.token is not None, seen.answered, seen.execution_ended) == ("active", True, None, False)
    assert {(t.key, t.status, t.generation) for t in view.tasks(run.name)} == {
        (run.execution_name("extract"), "running", 1), (run.execution_name("load"), "waiting", 1)}
    assert [(e.task, e.generation, e.running) for e in view.run_task_executions()] == [
        (run.execution_name("extract"), 1, True)]
    assert [(s.task, s.generation, s.row_status, s.row_generation) for s in view.starts()] == [
        (run.execution_name("extract"), 1, "deps_ready", 1)]


def test_each_write_of_a_row_is_seen_with_its_writer_and_its_conditions(world):
    run = _run(world)
    extract = run.execution_name("extract")
    writes = [w for w in world.rows_view().row_writes() if w.task == extract]
    assert [(w.kind, w.status_before, w.status_after) for w in writes] == [
        ("start_run", None, "waiting"), ("decision", "waiting", "deps_ready"), ("run_task", "deps_ready", "running")]
    decision, claim = writes[1], writes[2]
    assert (decision.conditional, decision.checks_run_version) == (True, True)
    assert (claim.conditional, claim.writer_generation) == (True, 1)


def test_an_answer_records_the_rows_that_were_not_final_and_a_pass_its_work(world):
    run = _run(world)
    world.complete(run, "extract")
    world.deliver_task_hints()
    world.complete(run, "load")
    world.deliver_task_hints()
    view = world.rows_view()
    assert [(a.run, a.unfinished) for a in view.answers()] == [(run.name, ())]
    assert sum(p.starts for p in view.passes()) == 2 and sum(p.answers for p in view.passes()) == 1
    assert sum(p.closes for p in view.passes()) == 1
    [seen] = view.runs()
    assert seen.answered == seen.token and seen.execution_ended


def test_a_write_that_takes_a_row_out_of_its_wait_without_the_dispatcher_is_caught(world):
    run = _run(world)
    with acting_as("lambda:rogue"):
        world.ddb.update_item(TableName=world.stack.tables["PipelineTokensTable"],
                              Key={"execution_name": {"S": run.execution_name("load")}},
                              UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                              ExpressionAttributeValues={":s": {"S": "deps_ready"}})
    found = CHECKS[2](world.rows_view())
    assert found == [f"invariant 2: {run.execution_name('load')} left waiting for deps_ready by a other write"]
    world.plant(found[0])
