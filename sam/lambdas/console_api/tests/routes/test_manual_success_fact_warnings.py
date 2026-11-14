"""Mark Successful turns a fact it cannot record into a `fact` warning (Principle #38).

A fact is state, not a hint: nothing re-checks a fact that was never written,
so the tasks waiting on it would hang silently. DynamoDB is moto with the
tables of sam/template.yaml; the asset-events write is made to fail.
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

ITEM = {"execution_name": "extract-2026-11-12--2026-11-12-abcdef12", "pipeline_name": "orders",
        "pipeline_execution": "orders-run-2026-11-12-abcdef12", "date": "2026-11-12"}
NODES = [{"id": "extract", "outlets": [{"name": "lake/sales"}, {"name": "lake/prices"}]}]


@pytest.fixture
def tables(mocker):
    import config
    from dal import executions_repo, pipelines_repo

    (tokens_logical, tokens_name), (registry_logical, registry_name) = kit.TABLES["TOKENS_TABLE"], kit.TABLES["PIPELINES_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {tokens_logical: tokens_name, registry_logical: registry_name})
        mocker.patch.object(config, "_dynamodb", resource)
        mocker.patch.object(executions_repo, "_table_name", tokens_name)
        mocker.patch.object(pipelines_repo, "_table_name", registry_name)
        resource.Table(registry_name).put_item(Item={"pipeline_name": "orders", "dag": json.dumps({"nodes": NODES})})
        yield resource.Table(tokens_name)


def _warning(tokens, asset):
    return tokens.get_item(Key={"execution_name": f"_notify_warn_fact_{ITEM['execution_name']}-fact-{asset}"}).get("Item")


def test_a_fact_that_cannot_be_recorded_becomes_a_fact_warning(tables, mocker):
    from routes import tasks

    def write_fact(*, asset_name, **_):
        if asset_name == "lake/sales":
            raise RuntimeError("AccessDeniedException")

    mocker.patch.object(tasks.asset_events_repo, "write_fact", side_effect=write_fact)

    assert tasks.record_manual_success_facts(dict(ITEM), "extract") == {"lake/sales": "AccessDeniedException"}

    warning = _warning(tables, "lake/sales")
    assert (warning["kind"], warning["task_name"], warning["task_date"]) == ("fact", "extract", "2026-11-12")
    assert "fact of asset lake/sales was not recorded: AccessDeniedException" in warning["error"]
    assert warning["attention"].startswith("warn#")
    assert _warning(tables, "lake/prices") is None            # recorded


def test_the_runs_own_outlets_come_from_the_task_row(tables, mocker):
    """A wrapper step is no node of the registry's dag: its row says it has no outlets, so nothing is
    owed and nothing is warned; a row that names outlets is used as it is, without the registry."""
    from routes import tasks

    write_fact = mocker.patch.object(tasks.asset_events_repo, "write_fact")
    step = {**ITEM, "execution_name": "notify_team-2026-11-12--2026-11-12-abcdef12", "outlets": "[]"}
    assert tasks.record_manual_success_facts(step, "notify_team") == {}
    reader = mocker.patch.object(tasks, "_read_dag_node")
    assert tasks.record_manual_success_facts({**ITEM, "outlets": json.dumps([{"name": "lake/orders"}])}, "extract") == {}
    reader.assert_not_called()
    assert [c.kwargs["asset_name"] for c in write_fact.call_args_list] == ["lake/orders"]
    assert not [i for i in tables.scan()["Items"] if i["execution_name"].startswith("_notify_warn_")]


def test_a_row_without_a_date_warns_for_every_outlet(tables, mocker):
    from routes import tasks

    write = mocker.patch.object(tasks.asset_events_repo, "write_fact")
    item = {k: v for k, v in ITEM.items() if k != "date"}

    assert set(tasks.record_manual_success_facts(item, "extract")) == {"lake/sales", "lake/prices"}
    write.assert_not_called()
    assert _warning(tables, "lake/prices")["error"].startswith("Marked successful, but the fact of asset lake/prices")


@pytest.mark.parametrize("registry,reason", [
    ("unreadable", "the pipeline registry could not be read"),
    ("missing", "pipeline orders is not in the registry"),
    ("no node", "task extract is not in the deployed pipeline orders"),
])
def test_outlets_that_cannot_be_read_are_a_warning_too(tables, mocker, registry, reason):
    """B-M2: when the registry cannot say which facts the task produces, the missing facts are not silent."""
    from botocore.exceptions import ClientError
    from routes import tasks

    if registry == "unreadable":
        mocker.patch.object(tasks.pipelines_repo, "get",
                            side_effect=ClientError({"Error": {"Code": "ThrottlingException"}}, "GetItem"))
    elif registry == "missing":
        mocker.patch.object(tasks.pipelines_repo, "get", return_value=None)
    else:
        mocker.patch.object(tasks.pipelines_repo, "get", return_value={"dag": json.dumps({"nodes": []})})
    write = mocker.patch.object(tasks.asset_events_repo, "write_fact")

    failed = tasks.record_manual_success_facts(dict(ITEM), "extract")

    assert list(failed) == ["*"] and failed["*"].startswith(reason)
    write.assert_not_called()
    warning = _warning(tables, "*")
    assert warning["error"].startswith("Marked successful, but no fact was recorded: its outlets are unknown")
