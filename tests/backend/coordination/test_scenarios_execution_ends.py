"""The end of an execution on EventBridge wakes its run at once (ADR #137 §7, ExecutionEndRule).

An execution that ends without a write (stopped in the AWS console, timed out,
failed outside its catchers) was seen only at the sweeper's check, up to about
35 minutes later. The rule's event is a hint: the pass it wakes decides as that
check does, from the rows and DescribeExecution, and a lost event costs the
check. The World delivers each event the rule lets through whenever the stack
settles, as EventBridge does within seconds (event_bus.py).
"""

import pytest

from dags import chain

DATE = "2026-11-25"
defect = pytest.mark.defect


@pytest.fixture(autouse=True)
def _tasks_stream_runs(world):
    world.tasks_stream_runs = True


def another_platforms(event, run):
    """The same end, of an execution of the run's name in another platform's pipeline of the account."""
    detail = dict(event["detail"])
    machine = detail["stateMachineArn"].replace(":stateMachine:acme-dev-", ":stateMachine:other-qa-")
    detail.update(stateMachineArn=machine, executionArn=f"{machine.replace(':stateMachine:', ':execution:')}:{run.name}")
    return {**event, "id": "another", "detail": detail}


@defect("R-V1")
@pytest.mark.parametrize("error", ["Console", ""])
def test_a_run_stopped_in_the_aws_console_is_stopped_on_its_end_before_any_sweep(world, error):
    """A stop with an error, and one without, whose event carries "error": null."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    assert world.running_tasks(run) == ["extract"]
    world.stop_run_externally(run, error=error)
    assert [world.status(run, task) for task in ("extract", "load")] == ["stopped", "stopped"]
    assert "its execution ended ABORTED" in world.row(run, "extract")["error"]
    assert len(world.execution_ends(run)) == 1


def test_a_run_whose_end_is_lost_is_stopped_at_its_check(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.lose_execution_end(run)
    world.stop_run_externally(run)
    # The premise: the execution ended while its rows ran (invariant 8); the engine stops them at its check.
    world.plant(f"invariant 8: the execution of {run.name} ended while {run.execution_name('extract')}, "
                f"{run.execution_name('load')} were not final")
    assert world.status(run, "extract") == "running" and world.execution_ends(run) == []
    world.clock.advance(minutes=31)
    world.sweep()
    assert [world.status(run, task) for task in ("extract", "load")] == ["stopped", "stopped"]


def test_an_end_that_comes_twice_or_from_another_platform_changes_nothing(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.stop_run_externally(run)
    [end] = world.execution_ends(run)
    rows = {task: world.row(run, task) for task in ("extract", "load")}
    assert world.send_execution_end(end)["action"] == "ignored"                   # the run is stopped already
    assert world.send_execution_end(another_platforms(end, run))["action"] == "ignored"
    assert {task: world.row(run, task) for task in ("extract", "load")} == rows


def test_another_platforms_end_of_a_run_of_the_same_name_leaves_the_run_running(world):
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    other = world.start_run(world.deploy(chain("other", "work")), DATE)
    world.stop_run_externally(other)
    [end] = world.execution_ends(other)
    forged = another_platforms(end, run)
    assert world.send_execution_end(forged)["reason"] == "another execution than the run's"
    assert world.running_tasks(run) == ["extract"]


def test_the_engines_own_stop_of_a_run_makes_no_event(world):
    """A Stop of a large run stops each running row's run_task and then the run's execution, each with
    Polyris.Stopped: the rule lets none of them through, so a Stop makes no invocation per row."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.stop_run(run)
    assert [world.status(run, task) for task in ("extract", "load")] == ["stopped", "stopped"]
    ended = world.executions_ended(run) + world.executions_ended(run, "extract")
    assert ended and {e["detail"]["error"] for e in ended} == {"Polyris.Stopped"}
    assert world.execution_ends(run) == [] and world.execution_ends(run, "extract") == []


def test_a_failed_run_ends_with_an_event_that_finds_it_answered(world):
    """A failed run's execution ends FAILED with Polyris.RunFailed after the engine's answer: its event costs one
    read of the run row and wakes no pass."""
    run = world.start_run(world.deploy(chain("orders", "extract")), DATE)
    world.complete(run, "extract", status="failed")
    world.fail_task(run, "extract")                      # the operator's decision on the failed task
    assert world.pipeline_status(run) == "FAILED"
    [end] = world.execution_ends(run)
    assert end["detail"]["error"] == "Polyris.RunFailed"
    assert world.send_execution_end(end)["reason"] == "a run answered or stopped already"


def test_the_streams_passes_make_no_describe_execution(world):
    """ADR #137 §1: a stream or narrow pass never judges an execution; the sweeper's and an end's passes do."""
    run = world.start_run(world.deploy(chain("orders", "extract", "load")), DATE)
    world.complete(run, "extract")
    world.stop_run_externally(run)
    stopped = world.start_run(world.deploy(chain("lost", "work")), DATE)
    world.lose_execution_end(stopped)
    world.stop_run_externally(stopped)
    world.plant(f"invariant 8: the execution of {stopped.name} ended while {stopped.execution_name('work')} "
                "were not final")
    world.clock.advance(minutes=31)
    world.sweep()
    sources = {source for source, _ in world.describes()}
    assert {"event", "sweeper"} <= sources and "stream" not in sources and None not in sources
