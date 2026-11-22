"""The console's Skip, Fail, Mark Successful and Stop of a task (ADR #137 §3), on moto tables.

The route reads the task row, its run row, the date's output# and the task's spec, and sends the transaction
coordination.actions builds; Step Functions is the only thing replaced (for the stop of a running task).
"""

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from polyris.coordination import attention, run_input, run_row, task_row, task_spec
from polyris.coordination.times import format_utc

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

RUN = "orders-run-2026-11-21-abcd1234"
KEY = "load-2026-11-21--2026-11-21-abcd1234"
OUTPUT = "output#orders#load#2026-11-21"
ARN = "arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-run-task-helper:load-x"


@pytest.fixture
def world(mocker):
    import config
    from dal import executions_repo
    from dal.task_events_repo import task_events_repo
    from routes import tasks
    from tests.specs_kit import deploy_specs, spec, use_specs_table

    names = {kit.TABLES[k][0]: kit.TABLES[k][1] for k in ("TOKENS_TABLE", "ASSET_EVENTS_TABLE", "TASK_EVENTS_TABLE",
                                                          "PIPELINES_TABLE")}
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, names)
        mocker.patch.object(config, "_dynamodb", resource)
        use_specs_table(mocker, resource)
        tokens, facts = kit.TABLES["TOKENS_TABLE"][1], kit.TABLES["ASSET_EVENTS_TABLE"][1]
        mocker.patch.object(executions_repo, "_table_name", tokens)
        mocker.patch.object(task_events_repo, "_table_name", kit.TABLES["TASK_EVENTS_TABLE"][1])
        mocker.patch.object(tasks.asset_events_repo, "_table_name", facts)
        from dal import pipelines_repo
        mocker.patch.object(pipelines_repo, "_table_name", kit.TABLES["PIPELINES_TABLE"][1])
        sfn = mocker.patch.object(tasks, "sfn")                    # the boundary: Step Functions
        registry = deploy_specs(resource, "orders", [spec("load", outlets=[{"name": "ns/orders"}])])
        resource.Table(kit.TABLES["PIPELINES_TABLE"][1]).put_item(Item={"pipeline_name": "orders", **registry})
        table = resource.Table(tokens)
        yield type("World", (), {"table": table, "tasks": tasks, "sfn": sfn, "resource": resource, "registry": registry,
                                 "facts": resource.Table(facts), "events": resource.Table(kit.TABLES["TASK_EVENTS_TABLE"][1])})


def seed(world, status: str, *, generation: int = 2, run_row_too: bool = True, started: timedelta = timedelta(hours=1),
         cascade: str = "auto", **fields):
    world.table.put_item(Item={
        task_row.TABLE_KEY: KEY, task_row.TASK_NAME: "load", task_row.STATUS: status, task_row.GENERATION: generation,
        "pipeline_name": "orders", "pipeline_execution": RUN, "date": "2026-11-21",
        task_row.READINESS: task_spec.canonical_json({"trigger_rule": "all_success", "dependencies": []}), **fields})
    if run_row_too:
        world.table.put_item(Item={run_row.TABLE_KEY: run_row.key(RUN), run_row.EXECUTION: RUN,
                                   run_row.STATUS: run_row.ACTIVE, run_row.RUN_VERSION: 4,
                                   run_row.RUN_START: "2026-11-21T06:00:00.000Z", run_row.RUN_DATE: "2026-11-21",
                                   run_row.PIPELINE: "orders", run_row.SPEC_ID: world.registry["spec_id"],
                                   run_row.TASK_COUNT: world.registry["task_count"],
                                   run_row.STARTED_AT: format_utc(datetime.now(timezone.utc) - started)})
        world.table.put_item(Item=run_input.to_item(run_input.RunInput(partition_arg="2026-11-21", cascade=cascade),
                                                    pipeline_execution=RUN, ttl=1))


def act(world, route: str, **body):
    return getattr(world.tasks, route)(KEY, {"body": json.dumps({"pipeline_execution": RUN, "date": "2026-11-21",
                                                                  **body})})


def row(world):
    return world.table.get_item(Key={task_row.TABLE_KEY: KEY}, ConsistentRead=True)["Item"]


@pytest.mark.parametrize("route, status", [("skip_task", "skipped"), ("fail_task", "failed"),
                                           ("mark_success", "success"), ("stop_task", "stopped")])
def test_an_action_moves_the_row_raises_its_run_and_leaves_its_marker_and_events(world, route, status):
    seed(world, "waiting_decision")
    response = act(world, route, reason="checked by hand")
    assert response["statusCode"] == 200, response
    assert (row(world)["status"], row(world)["generation"]) == (status, 3)
    run = world.table.get_item(Key={run_row.TABLE_KEY: run_row.key(RUN)})["Item"]
    assert run[run_row.RUN_VERSION] == 5 and attention.DUE in run
    marker = json.loads(world.table.get_item(Key={task_row.TABLE_KEY: OUTPUT})["Item"]["result"])
    assert marker["_manually_resolved"] is True and marker["_reason"] == "checked by hand"
    events = world.events.scan()["Items"]
    assert [e["event_type"] for e in events] == ["MANUAL_DECISION"]


def test_mark_successful_records_the_facts_of_the_tasks_outlets(world):
    seed(world, "failed", error="boom")
    assert act(world, "mark_success")["statusCode"] == 200
    [fact] = world.facts.scan()["Items"]
    assert (fact["asset_name"], fact["partition_key"], fact["source_task"]) == ("ns/orders", "2026-11-21", "load")
    assert sorted(e["event_type"] for e in world.events.scan()["Items"]) == ["MANUAL_DECISION", "TASK_FINISHED"]


def test_mark_successful_records_its_facts_with_the_runs_cascade(world):
    """A backfill with cascade='none' starts no consumers from a fact Mark Successful records (B-M3)."""
    seed(world, "failed", cascade="none")
    assert act(world, "mark_success")["statusCode"] == 200
    [fact] = world.facts.scan()["Items"]
    assert fact["cascade"] == "none"


def test_mark_successful_whose_outlets_cannot_be_read_is_refused_with_nothing_recorded(world, mocker):
    """Its facts belong to the success (invariant 12): no success without them."""
    seed(world, "failed")
    mocker.patch.object(world.tasks, "_run_task_spec", return_value=(None, "the task specs cannot be read"))
    response = act(world, "mark_success")
    assert response["statusCode"] == 409 and "outlets cannot be read" in json.loads(response["body"])["error"]
    assert row(world)["status"] == "failed" and not world.facts.scan()["Items"]


def test_mark_successful_records_the_outlets_of_its_runs_spec_not_of_a_redeploy_since(world):
    """Review of TR-6, B-2: the pipeline was redeployed with another outlet, and the task renamed away."""
    from tests.specs_kit import deploy_specs, spec
    seed(world, "failed")
    newer = deploy_specs(world.resource, "orders", [spec("loader", outlets=[{"name": "ns/other"}])])
    world.resource.Table(kit.TABLES["PIPELINES_TABLE"][1]).put_item(Item={"pipeline_name": "orders", **newer})
    assert act(world, "mark_success")["statusCode"] == 200
    assert [f["asset_name"] for f in world.facts.scan()["Items"]] == ["ns/orders"]


@pytest.mark.parametrize("route", ["skip_task", "fail_task", "mark_success", "stop_task"])
def test_an_action_on_a_running_task_stops_its_run_task(world, route):
    """ADR #137 §3: whatever the action, the work of a task that runs stops (review of TR-6, B-1)."""
    seed(world, "running", run_task_helper_arn=ARN)
    assert act(world, route)["statusCode"] == 200
    assert world.sfn.stop_execution.call_args.kwargs["executionArn"] == ARN


def test_a_stop_of_a_running_task_stops_its_run_task(world):
    seed(world, "running", run_task_helper_arn=ARN)
    assert act(world, "stop_task")["statusCode"] == 200
    world.sfn.stop_execution.assert_called_once()
    assert world.sfn.stop_execution.call_args.kwargs["executionArn"] == ARN


def test_an_action_the_status_does_not_allow_is_refused(world):
    seed(world, "success")
    response = act(world, "skip_task")
    assert response["statusCode"] == 409 and "takes no skip" in json.loads(response["body"])["error"]
    assert row(world)["status"] == "success"


def test_a_task_of_a_run_from_before_1_3_takes_no_action(world):
    seed(world, "failed", run_row_too=False)
    response = act(world, "mark_success")
    assert response["statusCode"] == 409 and "before 1.3" in json.loads(response["body"])["error"]


def test_a_row_that_moves_under_the_action_is_read_again_and_the_action_applies(world):
    """A pass raised the row's generation after the route's read: the transaction is cancelled, built again from
    the new read, and applies (ADR #137 §3)."""
    import config
    seed(world, "waiting_decision")
    client = config._dynamodb_client                     # the low-level client the repository sends it with
    moved = []

    def a_pass_moves_the_row(params, **_):
        if not moved:
            moved.append(1)
            world.table.update_item(Key={task_row.TABLE_KEY: KEY}, UpdateExpression="SET generation = :g",
                                    ExpressionAttributeValues={":g": 7})

    client.meta.events.register("before-parameter-build.dynamodb.TransactWriteItems", a_pass_moves_the_row)
    assert act(world, "fail_task")["statusCode"] == 200
    assert (row(world)["status"], row(world)["generation"]) == ("failed", 8)


def test_real_output_keeps_the_marker_out_and_warns(world):
    seed(world, "failed")
    world.table.put_item(Item={task_row.TABLE_KEY: OUTPUT, "result": json.dumps({"rows": 3})})
    assert act(world, "skip_task")["statusCode"] == 200
    assert json.loads(world.table.get_item(Key={task_row.TABLE_KEY: OUTPUT})["Item"]["result"]) == {"rows": 3}
    warnings = [i for i in world.table.scan()["Items"] if str(i["execution_name"]).startswith("_notify_warn_manual")]
    assert warnings and "real output" in warnings[0]["error"]


def test_a_row_that_finished_under_the_action_is_refused_with_nothing_after_it(world):
    """run_task recorded the task's success after the route's read: the transaction is cancelled, the new read
    allows no Stop, and nothing follows (no event, no StopExecution, no marker)."""
    import config
    seed(world, "running", run_task_helper_arn=ARN)

    def run_task_succeeds(params, **_):
        world.table.update_item(Key={task_row.TABLE_KEY: KEY}, UpdateExpression="SET #s = :s",
                                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":s": "success"})

    config._dynamodb_client.meta.events.register("before-parameter-build.dynamodb.TransactWriteItems",
                                                 run_task_succeeds)
    response = act(world, "stop_task")
    assert response["statusCode"] == 409 and "takes no stop" in json.loads(response["body"])["error"]
    assert row(world)["status"] == "success" and not world.events.scan()["Items"]
    world.sfn.stop_execution.assert_not_called()
    assert "Item" not in world.table.get_item(Key={task_row.TABLE_KEY: OUTPUT})


# ── Restart (ADR #137 §3) ──────────────────────────────────────────────────

def run_item(world):
    return world.table.get_item(Key={run_row.TABLE_KEY: run_row.key(RUN)}, ConsistentRead=True)["Item"]


@pytest.mark.parametrize("status", ["failed", "success", "skipped", "upstream_failed", "stopped", "waiting_decision"])
def test_a_restart_puts_a_final_or_undecided_row_back_to_waiting_and_requests_the_renewal(world, status):
    """A stopped task too: Stop's own message tells the operator to restart it."""
    seed(world, status, error="boom", run_task_helper_arn=ARN, attempt=1)
    response = act(world, "restart_task")
    assert response["statusCode"] == 200, response
    written = row(world)
    assert (written["status"], written["generation"], written["attempt"]) == ("waiting", 3, 1)
    assert "error" not in written and task_row.RUN_TASK_ARN not in written
    assert set(run_item(world)[run_row.REQUESTS]) == {run_row.RENEW_TTL}
    assert [e["decision"] for e in world.events.scan()["Items"]] == ["restart"]
    world.sfn.start_execution.assert_not_called()         # the dispatcher starts it, from the stream


@pytest.mark.parametrize("status", ["waiting", "deps_ready", "running"])
def test_a_row_that_has_not_finished_takes_no_restart(world, status):
    seed(world, status)
    response = act(world, "restart_task")
    assert response["statusCode"] == 409 and "takes no restart" in json.loads(response["body"])["error"]


def test_a_restart_with_dependants_requests_their_reset(world):
    seed(world, "failed")
    response = act(world, "restart_task", with_dependants=True)
    assert response["statusCode"] == 200 and "with its dependants" in json.loads(response["body"])["message"]
    assert run_item(world)[run_row.REQUESTS][run_row.RESET_DEPENDANTS]["task"] == "load"


def test_a_restart_of_a_run_that_started_a_year_ago_or_before_1_3_is_refused(world):
    seed(world, "failed", started=timedelta(days=364, minutes=1))
    response = act(world, "restart_task")
    assert response["statusCode"] == 409 and "more than a year ago" in json.loads(response["body"])["error"]
    world.table.delete_item(Key={run_row.TABLE_KEY: run_row.key(RUN)})
    assert "before 1.3" in json.loads(act(world, "restart_task")["body"])["error"]


def test_a_restart_of_a_task_that_does_not_exist_is_404(world):
    response = world.tasks.restart_task("nope", {"body": json.dumps({"date": "2026-11-21"})})
    assert response["statusCode"] == 404
