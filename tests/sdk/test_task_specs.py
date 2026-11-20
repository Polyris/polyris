"""What the generator writes to ``pipeline-specs``, and the limits of a pipeline (ADR #137 §8).

Golden spec files (``tests/golden_specs``) show what a change to the generator
does to the specs. A change of their shape (a key, or the type of a value)
without a new ``task_spec.FORMAT_VERSION`` fails here even under
``SNAPSHOT_UPDATE=1``: ``Start_Run`` refuses a format it does not support, so
a new shape must come with a new format.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from polyris import DAG, Asset, GlueTask, Wait, task
from polyris import generators, limits
from polyris.constants import TaskSpecKey as K
from polyris.coordination import run_row, run_task_input, task_spec
from polyris.validation import validate_asl_from_dag

GOLDEN_DIR = Path(__file__).parent.parent / "golden_specs"
UPDATE = os.environ.get("SNAPSHOT_UPDATE", "").strip() in ("1", "true", "yes")
ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:"


def _every_field():
    """Every task type and every key of a spec, with a wrapper step and a direct step."""
    orders, sales, customers = Asset("s3://acme/orders/"), Asset("s3://acme/sales/"), Asset("s3://acme/customers/")
    with DAG("every_field", schedule=None) as dag:
        @task.sfn(arn=ARN + "extract", outlets=[orders], wait_for=[customers.within(hours=24)],
                  orchestration_timeout=timedelta(days=8), skip_on_backfill=True)
        def extract():
            pass

        @task.glue_job(job_name="load", retries=2, execution_timeout=timedelta(hours=2), inlets=[orders],
                       outlets=[sales], wait_before=30, trigger_rule="all_done",
                       wait_for=[customers.consecutive(days=3) | orders])
        def load():
            pass

        @task.lambda_function(function_name="notify", payload={"k": "Ladung für Kunden"})
        def notify():
            pass

        @task.ecs_task(cluster="c", task_definition="td", subnets=["s-1"])
        def ecs():
            pass

        @task.athena_query(query_string="SELECT 1", database="db", output_location="s3://acme/athena/")
        def athena():
            pass

        @task.emr_step(emr_cluster_id="j-1", emr_step={"Name": "spark", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["spark-submit"]}})
        def emr():
            pass

        @task.batch_job(job_definition="jd", job_queue="q")
        def batch():
            pass

        glue_step = GlueTask(step_id="compact", job_name="compact-job")
        Wait(step_id="pause", seconds=10)
        e = extract()
        e >> load() >> notify()
        e >> glue_step
        ecs(), athena(), emr(), batch()
    return dag


def _single():
    with DAG("single", schedule=None) as dag:
        @task.sfn(arn=ARN + "process")
        def process():
            pass
        process()
    return dag


GOLDEN = {"every_field": _every_field, "single": _single}


def _shape(value):
    """A value's shape: object keys and JSON types, not values."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return sorted({json.dumps(_shape(v), sort_keys=True) for v in value})
    return type(value).__name__


def _golden(version):
    return {"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id,
            "specs": {task_id: json.loads(text) for task_id, text in sorted(version.texts.items())}}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_golden_specs(name):
    path = GOLDEN_DIR / f"{name}.json"
    generated = _golden(generators.build_spec_version(GOLDEN[name]()))
    stored = json.loads(path.read_text()) if path.exists() else None
    if stored is not None and stored["format_version"] == task_spec.FORMAT_VERSION:
        assert _shape(generated["specs"]) == _shape(stored["specs"]), (
            f"The specs' shape changed without a new task_spec.FORMAT_VERSION ({path.name}). "
            "Start_Run refuses a format it does not support: bump FORMAT_VERSION, then regenerate.")
    if UPDATE:
        GOLDEN_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(generated, indent=2, ensure_ascii=False) + "\n")
        return
    assert stored == generated, f"specs of {name} changed: run SNAPSHOT_UPDATE=1 pytest {__file__}"


def test_the_shape_check_sees_a_new_key_and_a_new_type():
    base = {"t": {"task_id": "t", "wait_before": 30}}
    assert _shape(base) != _shape({"t": {"task_id": "t", "wait_before": 30, "pool": "a"}})
    assert _shape(base) != _shape({"t": {"task_id": "t", "wait_before": "30"}})
    assert _shape(base) == _shape({"t": {"task_id": "t", "wait_before": 45}})


# ── what the generator writes ────────────────────────────────────────


def test_every_spec_reads_back_with_the_parser_start_run_uses():
    version = generators.build_spec_version(_every_field())
    for task_id, text in version.texts.items():
        assert task_spec.parse(text, expected_hash=task_spec.text_hash(text)).task_id == task_id


def test_tasks_and_wrapper_steps_become_specs_and_direct_steps_do_not():
    version = generators.build_spec_version(_every_field())
    assert sorted(version.texts) == ["athena", "batch", "compact", "ecs", "emr", "extract", "load", "notify"]
    step = json.loads(version.texts["compact"])
    assert step[K.TASK_TYPE] == "glue" and step[K.DEPENDENCIES] == ["extract"]
    assert step[K.TRIGGER_RULE] == "all_success" and K.EXECUTION_TIMEOUT not in step


def test_a_task_without_limits_has_none_in_its_spec():
    """D49: no execution_timeout by default, and orchestration_timeout does not fall back to it."""
    spec = json.loads(generators.build_spec_version(_single()).texts["process"])
    assert K.EXECUTION_TIMEOUT not in spec and K.ORCHESTRATION_TIMEOUT not in spec
    load = json.loads(generators.build_spec_version(_every_field()).texts["load"])
    assert load[K.EXECUTION_TIMEOUT] == 7200 and K.ORCHESTRATION_TIMEOUT not in load


def test_a_step_without_a_row_has_no_config():
    with pytest.raises(ValueError, match="does not run as a task row"):
        generators._build_step_config_and_arn(Wait(step_id="pause", seconds=1))


def test_register_pipeline_names_the_version_and_its_task_count():
    dag = _every_field()
    version = generators.build_spec_version(dag)
    state = json.loads(generators.generate_step_function_json(dag))["States"]["Register_Pipeline"]
    values = state["Arguments"]["ExpressionAttributeValues"]
    assert values[":spec_id"] == {"S": version.spec_id} and values[":task_count"] == {"N": "8"}
    assert "spec_id = :spec_id" in state["Arguments"]["UpdateExpression"]
    assert [c["ErrorEquals"] for c in state["Catch"]] == [["States.DataLimitExceeded"], ["States.ALL"]]
    # The 1.2 engine reads the registry's tasks and dag until TR-4 and TR-8 (Principle #39).
    assert {":tasks", ":dag"} <= set(values)


def test_the_same_asset_twice_in_outlets_is_refused():
    orders = Asset("s3://acme/orders/")
    with DAG("dupes", schedule=None):
        with pytest.raises(ValueError, match=r"@task.glue_job: outlets of task 'load' name asset 'acme/orders' twice"):
            @task.glue_job(job_name="load", outlets=[orders, Asset("s3://acme/orders/")])
            def load():
                pass


# ── limits (§8), each naming the limit and the measured size ─────────


def _version(specs):
    return task_spec.build_version("p", specs)


def _spec(task_id, **extra):
    return {"task_id": task_id, "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success", **extra}


def test_tasks_per_pipeline():
    many = _version([_spec(f"t{i}") for i in range(task_spec.MAX_TASKS + 1)])
    assert limits.spec_errors(many) == [f"Pipeline 'p' has {task_spec.MAX_TASKS + 1} tasks, over the limit of "
                                        f"{task_spec.MAX_TASKS} tasks per pipeline"]
    assert limits.spec_errors(_version([_spec(f"t{i}") for i in range(task_spec.MAX_TASKS)])) == []


def test_outlets_per_task():
    outlets = [{"name": f"a{i}"} for i in range(task_spec.MAX_OUTLETS + 1)]
    assert limits.spec_errors(_version([_spec("t", outlets=outlets)])) == [
        f"Task 't' has {task_spec.MAX_OUTLETS + 1} outlets, over the limit of {task_spec.MAX_OUTLETS} outlets per task"]
    assert limits.spec_errors(_version([_spec("t", outlets=outlets[:-1])])) == []


def test_a_task_id_in_bytes():
    long_id = "ä" * 65  # 130 bytes, 65 characters
    assert limits.spec_errors(_version([_spec(long_id)])) == [
        f"Task '{long_id}': its task_id takes 130 bytes, over the limit of {run_row.TASK_ID_MAX} bytes"]


def test_a_spec_in_bytes_as_written():
    big = _spec("t", task_config={"payload": "x" * run_task_input.PAYLOAD_LIMIT})
    size = len(task_spec.canonical_json(big).encode())
    assert limits.spec_errors(_version([big])) == [
        f"Task 't': its spec takes {size:,} bytes, over the limit of {run_task_input.PAYLOAD_LIMIT:,} bytes "
        f"of a task's input"]


def test_the_specs_together_in_bytes_which_start_run_reads_at_once(monkeypatch):
    """D117: a pass keeps no spec text, but Start_Run reads the whole version."""
    monkeypatch.setattr(task_spec, "MAX_VERSION_BYTES", 400)
    specs = [_spec(f"t{i}") for i in range(5)]
    total = sum(len(task_spec.canonical_json(s).encode()) for s in specs)
    assert total > 400
    assert limits.spec_errors(_version(specs)) == [
        f"Pipeline 'p': its task specs take {total:,} bytes together, over the limit of 400 bytes (16 MB) that "
        f"Start_Run reads at a run's start"]
    assert limits.spec_errors(_version(specs[:1])) == []


def test_the_definition_is_measured_as_deploy_sends_it():
    """Indented, as generate_step_function_json writes it: measured compact, 230 tasks validated
    at 937 KB and deployed at 1.11 MB (ADR #137 §8)."""
    result = {f"k{i}": 0 for i in range(60000)}
    asl = {"StartAt": "S", "States": {"S": {"Type": "Pass", "Output": result, "End": True}}}
    assert len(json.dumps(asl).encode()) < limits.DEFINITION_MAX <= len(json.dumps(asl, indent=2).encode())
    ok, errors, _ = generators.validate_asl(asl)
    assert not ok and "exceeds the AWS 1 MB limit" in errors[0]


def test_a_short_orchestration_timeout_on_a_wait_for_is_a_warning():
    """D49, D63: a wait_for waits 7 days; an explicit cap under that ends it earlier."""
    short = _spec("t", orchestration_timeout=3600, wait_for=[{"asset_name": "a", "freshness_hours": None}])
    assert limits.spec_warnings(_version([short])) == [
        "Task 't': orchestration_timeout of 3600 seconds ends its wait_for wait before the 604800 seconds "
        "(7 days) a wait_for waits for its assets"]
    assert limits.spec_warnings(_version([{**short, "orchestration_timeout": 7 * 86400}])) == []
    assert limits.spec_warnings(_version([_spec("u", orchestration_timeout=60)])) == []


def test_validate_refuses_a_pipeline_beyond_a_limit_and_reports_the_warning(capsys):
    with DAG("over", schedule=None) as dag:
        @task.sfn(arn=ARN + "x", outlets=[Asset(f"s3://acme/a{i}/") for i in range(task_spec.MAX_OUTLETS + 1)])
        def wide():
            pass

        @task.sfn(arn=ARN + "y", wait_for=[Asset("s3://acme/in/")], orchestration_timeout=timedelta(days=1))
        def short_wait():
            pass
        wide(), short_wait()
    valid, errors, warnings = validate_asl_from_dag(dag, verbose=True)
    assert not valid
    assert errors == [f"Task 'wide' has {task_spec.MAX_OUTLETS + 1} outlets, over the limit of "
                      f"{task_spec.MAX_OUTLETS} outlets per task"]
    assert any("short_wait" in w for w in warnings)
    assert "Warnings (1)" in capsys.readouterr().out
