"""The console's one accessor of the task specs (ADR #137 §8), and the readers that moved to it.

DynamoDB is moto, with ``pipeline-specs`` as sam/template.yaml declares it; the
versions are written with the same ``spec_store.write`` that polyris-deploy uses.
"""
import json
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from polyris import DAG, Asset, GlueTask, task
from polyris import generators
from polyris.coordination import task_spec
from tests.specs_kit import SPECS_TABLE, deploy_specs, spec, use_specs_table


@pytest.fixture
def resource(mocker):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        use_specs_table(mocker, resource)
        yield resource


def _repo():
    import importlib
    return importlib.import_module("dal.specs_repo")


def test_a_pipeline_reads_its_tasks_from_the_version_the_registry_names(resource):
    registry = {"pipeline_name": "orders", **deploy_specs(resource, "orders", [
        spec("extract", skip_on_backfill=True), spec("load", dependencies=["extract"], outlets=[{"name": "sales"}])])}
    repo = _repo().specs_repo
    assert [t["task_id"] for t in repo.tasks_of(registry)] == ["extract", "load"]
    assert repo.task_of(registry, "load")["outlets"] == [{"name": "sales"}]
    assert repo.task_of(registry, "report") is None


def test_a_version_is_read_once_per_container(resource, mocker):
    registry = {"pipeline_name": "orders", **deploy_specs(resource, "orders", [spec("extract")])}
    repo = _repo().specs_repo
    read = mocker.spy(_repo().spec_store, "read")
    repo.tasks_of(registry)
    repo.tasks_of(registry)
    assert read.call_count == 1


def test_the_cache_keeps_the_latest_versions(resource, mocker):
    module = _repo()
    mocker.patch.object(module, "CACHE_VERSIONS", 2)
    names = [{"pipeline_name": f"p{i}", **deploy_specs(resource, f"p{i}", [spec("t")])} for i in range(3)]
    for registry in names + [names[2]]:
        module.specs_repo.tasks_of(registry)
    assert list(module.specs_repo._versions) == [names[1]["spec_id"], names[2]["spec_id"]]


def test_a_pipeline_deployed_before_1_3_names_no_specs(resource):
    with pytest.raises(_repo().SpecsMissing, match="deployed before 1.3; redeploy it"):
        _repo().specs_repo.tasks_of({"pipeline_name": "orders"})


def test_a_version_without_a_count_or_not_in_the_table_is_refused(resource):
    registry = {"pipeline_name": "orders", **deploy_specs(resource, "orders", [spec("extract")])}
    with pytest.raises(task_spec.SpecError, match="without a task count"):
        _repo().specs_repo.tasks_of({**registry, "task_count": None})
    with pytest.raises(task_spec.SpecError, match="has 1 task"):
        _repo().specs_repo.tasks_of({**registry, "task_count": 2})


@pytest.mark.parametrize("registered,needs", [
    (None, True), ("x", True), (True, True), (task_spec.FORMAT_VERSION - 1, True),
    (task_spec.FORMAT_VERSION, False), (str(task_spec.FORMAT_VERSION), False),
])
def test_a_pipeline_needs_a_redeploy_without_the_format_this_console_reads(registered, needs):
    item = {} if registered is None else {task_spec.REGISTRY_FORMAT_FIELD: registered}
    assert _repo().needs_redeploy(item) is needs


def test_the_pipelines_list_marks_a_pipeline_that_needs_a_redeploy(mocker):
    from routes import pipelines_list
    repo = mocker.patch.object(pipelines_list, "pipelines_repo")
    repo.list_all.return_value = [
        {"pipeline_name": "old"}, {"pipeline_name": "new", task_spec.REGISTRY_FORMAT_FIELD: task_spec.FORMAT_VERSION}]
    body = json.loads(pipelines_list.list_pipelines({"queryStringParameters": {}})["body"])
    assert {p["name"]: p["needs_redeploy"] for p in body["pipelines"]} == {"old": True, "new": False}


# ── the asset graph of backfill planning ─────────────────────────────


def test_deployed_tasks_reads_the_specs_and_skips_a_pipeline_not_redeployed(resource):
    from upstream_integration import build_asset_graph
    produced = {"pipeline_name": "raw", **deploy_specs(resource, "raw", [spec("t", outlets=[{"name": "lake/raw"}])])}
    graph = build_asset_graph([produced, {"pipeline_name": "old"}], _repo().specs_repo.deployed_tasks)
    assert graph.nodes["lake/raw"].pipeline == "raw"


# ── a pipeline's graph is drawn from its specs ────────────────────────


def _every_field_dag():
    orders, customers = Asset("s3://acme/orders/"), Asset("s3://acme/customers/")
    with DAG("drawn", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x", outlets=[orders],
                  wait_for=[customers.within(hours=24) | customers.consecutive(days=2), orders],
                  skip_on_backfill=True)
        def extract():
            pass

        @task.glue_job(job_name="load", retries=2, wait_before=timedelta(seconds=30), inlets=[orders],
                       trigger_rule="all_done", execution_timeout=timedelta(hours=1))
        def load():
            pass
        extract() >> load()
    return dag


def test_the_graph_from_specs_draws_each_task_as_the_generator_does():
    """Parity with generators._build_dag_visualization_nodes_edges, the graph 1.2 stored in the registry."""
    from routes.pipelines_info import graph_from_specs
    dag = _every_field_dag()
    version = generators.build_spec_version(dag)
    specs = {t: task_spec.parse(text, expected_hash=task_spec.text_hash(text)) for t, text in version.texts.items()}
    nodes, edges = generators._build_dag_visualization_nodes_edges(dag)
    expected_nodes = json.loads(json.dumps(sorted(nodes, key=lambda n: n["id"])))
    assert graph_from_specs(specs) == {"nodes": expected_nodes, "edges": edges}


def test_a_wrapper_step_is_drawn_as_a_task():
    from routes.pipelines_info import graph_from_specs
    with DAG("steps", schedule=None) as dag:
        GlueTask(step_id="compact", job_name="compact-job")
    version = generators.build_spec_version(dag)
    specs = {t: task_spec.parse(text, expected_hash=task_spec.text_hash(text)) for t, text in version.texts.items()}
    assert graph_from_specs(specs)["nodes"][0]["type"] == "task"


def _dag_route(mocker, registry):
    from routes import pipelines_info
    mocker.patch.object(pipelines_info.pipelines_repo, "get", return_value=registry)
    mocker.patch.object(pipelines_info.executions_repo, "rows_of_pipeline_date", return_value=[])
    response = pipelines_info.get_pipeline_dag("orders", {"queryStringParameters": {"date": "2026-11-18"}})
    return json.loads(response["body"])


def test_the_pipeline_dag_route_draws_the_registrys_version(resource, mocker):
    registry = {"pipeline_name": "orders", **deploy_specs(resource, "orders", [
        spec("extract"), spec("load", dependencies=["extract"])])}
    body = _dag_route(mocker, registry)
    assert body["dag_source"] == "registry"
    assert [n["id"] for n in body["nodes"]] == ["extract", "load"]
    assert body["edges"] == [{"from": "extract", "to": "load"}]


@pytest.mark.parametrize("registry", [
    {"pipeline_name": "orders"},                                               # deployed before 1.3
    {"pipeline_name": "orders", "spec_id": "orders#" + "0" * 64, "task_count": 1},  # not in the table
])
def test_specs_that_cannot_be_drawn_fall_back_to_the_runs(resource, mocker, registry):
    assert _dag_route(mocker, registry)["nodes"] == []


def test_the_specs_table_is_the_one_the_template_names(resource):
    assert SPECS_TABLE in resource.meta.client.list_tables()["TableNames"]
