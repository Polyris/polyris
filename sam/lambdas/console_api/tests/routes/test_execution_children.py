"""/api/execution-children lists a run's tasks, whatever other rows share the run.

A run's DAG snapshot row carries its pipeline_execution, so the run query returns
it, and a row without started_at failed the sort: the route answered [] for every
run (external review, round 2). DynamoDB is moto with the tables of
sam/template.yaml.
"""
import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

RUN = "orders-run-2026-11-12-abcd1234"


@pytest.fixture
def tokens(mocker):
    import config
    from dal import executions_repo

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(executions_repo, "_table_name", name)
        yield resource.Table(name)


def test_the_children_are_the_run_s_tasks_in_start_order(tokens):
    from routes import executions

    for item in (
        {"execution_name": f"load-2026-11-12-{RUN[-20:]}", "task_name": "load", "status": "waiting",
         "started_at": "2026-11-12T06:00:02Z"},
        {"execution_name": f"dag_snapshot::{RUN}", "dag": "{}"},
        {"execution_name": f"extract-2026-11-12-{RUN[-20:]}", "task_name": "extract", "status": "waiting"},
        {"execution_name": f"_notify_warn_notify_extract-2026-11-12-{RUN[-20:]}", "status": "failed"},
    ):
        tokens.put_item(Item={**item, "pipeline_execution": RUN})

    body = json.loads(executions.get_execution_children(RUN, {})["body"])

    assert [c["task_name"] for c in body["children"]] == ["extract", "load"]
