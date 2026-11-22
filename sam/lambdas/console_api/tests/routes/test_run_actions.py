"""The console's Stop, Pause and Resume of a run (ADR #137 §4, §5), on a moto tokens table.

Each writes the run row only; the dispatcher carries the stop out and holds or releases the rows (a harness
scenario follows each through the engine).
"""

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from polyris.coordination import attention, dispatch, run_row

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

RUN = "orders-run-2026-11-21-abcd1234"


@pytest.fixture
def world(mocker):
    import config
    from dal import executions_repo
    from routes import executions

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(config, "_dynamodb_client", boto3.client("dynamodb", region_name="us-east-1"))
        mocker.patch.object(executions_repo, "_table_name", name)
        yield type("World", (), {"table": resource.Table(name), "routes": executions})


def seed(world, **fields):
    world.table.put_item(Item={run_row.TABLE_KEY: run_row.key(RUN), run_row.EXECUTION: RUN,
                               run_row.STATUS: run_row.ACTIVE, run_row.RUN_VERSION: 4, **fields})


def the_run(world):
    return world.table.get_item(Key={run_row.TABLE_KEY: run_row.key(RUN)}, ConsistentRead=True)["Item"]


def call(world, route: str, **body):
    response = getattr(world.routes, route)(RUN, {"body": json.dumps(body)})
    return response["statusCode"], json.loads(response["body"])


def test_a_stop_writes_the_stop_mark_with_who_asked_and_why_and_a_second_changes_nothing(world):
    seed(world)
    status, body = call(world, "stop_execution", reason="wrong input")
    assert (status, body["stopping"]) == (200, True)
    run = the_run(world)
    assert (run[run_row.STOP]["reason"], run[run_row.RUN_VERSION]) == ("wrong input", 5) and attention.DUE in run
    assert call(world, "stop_execution")[0] == 200 and the_run(world)[run_row.RUN_VERSION] == 5


def test_a_pause_and_a_resume_open_and_close_an_interval_and_repeat_safely(world):
    seed(world)
    assert call(world, "pause_execution") == (200, {"message": "Pipeline execution paused", "paused": True,
                                                    "pipeline_execution": RUN})
    assert dispatch.run_view(the_run(world)).paused
    assert call(world, "pause_execution")[0] == 200 and the_run(world)[run_row.RUN_VERSION] == 5
    assert call(world, "resume_execution")[1]["paused"] is False
    assert not dispatch.run_view(the_run(world)).paused and the_run(world)[run_row.RUN_VERSION] == 6


def test_a_run_without_its_run_row_or_not_active_yet_is_refused(world):
    status, body = call(world, "pause_execution")
    assert status == 409 and "before 1.3" in body["error"]
    seed(world, **{run_row.STATUS: run_row.CREATING})
    assert call(world, "stop_execution")[0] == 409
    assert world.routes.stop_execution("", {"body": "{}"})["statusCode"] == 400


def test_a_change_of_the_run_row_under_the_write_is_read_again(world):
    """A Restart with dependants between the console's read and its write: the Stop, refused, is built again from
    the new read and cancels the reset that came meanwhile."""
    import config
    seed(world)
    landed = []

    def a_restart_lands(params, **_):
        if not landed:
            landed.append(1)
            world.table.update_item(Key={run_row.TABLE_KEY: run_row.key(RUN)},
                                    UpdateExpression="SET #rv = :rv", ExpressionAttributeNames={"#rv": "run_version"},
                                    ExpressionAttributeValues={":rv": 9})
            world.table.update_item(Key={run_row.TABLE_KEY: run_row.key(RUN)},
                                    UpdateExpression="SET #r = :r", ExpressionAttributeNames={"#r": "requests"},
                                    ExpressionAttributeValues={":r": {run_row.RESET_DEPENDANTS: {"by": "a", "at": "t"}}})

    config._dynamodb_client.meta.events.register("before-parameter-build.dynamodb.TransactWriteItems", a_restart_lands)
    assert call(world, "stop_execution")[0] == 200
    run = the_run(world)
    assert run_row.STOP in run and run[run_row.REQUESTS] == {}          # the reset that came meanwhile is cancelled
