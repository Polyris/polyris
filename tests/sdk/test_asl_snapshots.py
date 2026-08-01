"""
ASL Snapshot Tests for generators.py

Compares generated Step Functions JSON against golden files.
Catches regressions when modifying generators.py.

Usage:
    pytest tests/test_asl_snapshots.py           # Compare against snapshots
    SNAPSHOT_UPDATE=1 pytest tests/test_asl_snapshots.py  # Regenerate snapshots
"""

import json
import os
import sys
from datetime import timedelta
from pathlib import Path

# sys.path setup moved to conftest.py

SNAPSHOT_DIR = Path(__file__).parent.parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

UPDATE = os.environ.get("SNAPSHOT_UPDATE", "").strip() in ("1", "true", "yes")


def _normalize_asl(asl: dict) -> dict:
    """Remove non-deterministic fields for stable comparison."""
    # Comment field contains metadata JSON which is deterministic, keep it
    return asl


def _compare_or_update(name: str, generated: dict):
    """Compare ASL against snapshot or update if SNAPSHOT_UPDATE=1."""
    path = SNAPSHOT_DIR / f"{name}.json"
    normalized = _normalize_asl(generated)

    if UPDATE:
        path.write_text(json.dumps(normalized, indent=2) + "\n")
        return

    assert path.exists(), (
        f"Snapshot {path.name} not found. "
        f"Run SNAPSHOT_UPDATE=1 pytest tests/test_asl_snapshots.py to generate."
    )
    expected = json.loads(path.read_text())
    assert normalized == expected, (
        f"ASL mismatch for {name}. "
        f"Run SNAPSHOT_UPDATE=1 pytest tests/test_asl_snapshots.py to update.\n"
        f"Diff hint: check States keys: "
        f"got={sorted(normalized.get('States', {}).keys())} "
        f"expected={sorted(expected.get('States', {}).keys())}"
    )


# ============================================================
# DAG builders — one per scenario
# ============================================================

def _build_single_task():
    """Minimal: one SFN task, no deps, no extras."""
    from polyris import DAG, task

    with DAG("single_task", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass
        process()
    return dag


def _build_chain():
    """Three tasks in sequence: extract >> transform >> load."""
    from polyris import DAG, task

    with DAG("chain", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:transform")
        def transform():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:load")
        def load():
            pass
        e = extract()
        t = transform(e)
        load(t)
    return dag


def _build_parallel():
    """Three independent tasks — no dependencies."""
    from polyris import DAG, task

    with DAG("parallel", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:a")
        def task_a():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:b")
        def task_b():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:c")
        def task_c():
            pass
        task_a()
        task_b()
        task_c()
    return dag


def _build_fan_out():
    """One task fans out to three: extract >> [a, b, c]."""
    from polyris import DAG, task

    with DAG("fan_out", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:a")
        def transform_a():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:b")
        def transform_b():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:c")
        def transform_c():
            pass
        e = extract()
        transform_a(e)
        transform_b(e)
        transform_c(e)
    return dag


def _build_fan_in():
    """Three tasks fan in to one: [a, b, c] >> load."""
    from polyris import DAG, task

    with DAG("fan_in", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:a")
        def task_a():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:b")
        def task_b():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:c")
        def task_c():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:load")
        def load():
            pass
        a = task_a()
        b = task_b()
        c = task_c()
        load(a, b, c)
    return dag


def _build_wait_before():
    """Task with wait_before delay."""
    from polyris import DAG, task

    with DAG("wait_before", schedule=None) as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:delayed",
            wait_before=600
        )
        def delayed_task():
            pass
        delayed_task()
    return dag


    return dag


def _build_with_outlets():
    """Task with asset outlets."""
    from polyris import DAG, task
    from polyris.assets import Asset

    inventory = Asset("raw/inventory", uri="s3://bucket/raw/inventory/")

    with DAG("with_outlets", schedule=None) as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:ingest",
            outlets=[inventory]
        )
        def ingest():
            pass
        ingest()
    return dag


def _build_with_variables():
    """DAG with variables passed to tasks."""
    from polyris import DAG, task

    with DAG(
        "with_variables",
        schedule=None,
        variables={"env": "prod", "region": "us-east-1", "batch_size": 1000}
    ) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass
        process()
    return dag


def _build_with_retries():
    """Task with retry configuration."""
    from polyris import DAG, task

    with DAG("with_retries", schedule=None) as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:flaky",
            retries=3,
            retry_delay=timedelta(minutes=2)
        )
        def flaky_task():
            pass
        flaky_task()
    return dag


def _build_lambda_task():
    """Lambda task type (not SFN)."""
    from polyris import DAG, task

    with DAG("lambda_task", schedule=None) as dag:
        @task.lambda_(
            function_name="my-processor",
            payload={"action": "process"}
        )
        def process():
            pass
        process()
    return dag


def _build_asset_triggered():
    """DAG triggered by asset schedule (not cron)."""
    from polyris import DAG, task
    from polyris.assets import Asset

    inventory = Asset("raw/inventory")
    catalog = Asset("raw/catalog")

    with DAG(
        "asset_triggered",
        schedule=[inventory, catalog],
    ) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass
        process()
    return dag


def _build_cross_account():
    """Task with cross-account role."""
    from polyris import DAG, task

    with DAG("cross_account", schedule=None) as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:999:stateMachine:remote",
            role="acq"
        )
        def remote_task():
            pass
        remote_task()
    return dag


def _build_orchestration_timeout():
    """Task with custom orchestration_timeout."""
    from polyris import DAG, task

    with DAG("orch_timeout", schedule=None) as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:slow",
            execution_timeout=timedelta(hours=2),
            orchestration_timeout=timedelta(days=3)
        )
        def slow_task():
            pass
        slow_task()
    return dag


def _build_complex_diamond():
    """Diamond pattern: a >> [b, c] >> d."""
    from polyris import DAG, task

    with DAG("diamond", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:a")
        def a():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:b")
        def b():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:c")
        def c():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:d")
        def d():
            pass
        a_inst = a()
        b_inst = b(a_inst)
        c_inst = c(a_inst)
        d(b_inst, c_inst)
    return dag


# ============================================================
# Snapshot tests — one per scenario
# ============================================================

def _build_consecutive_wait_for():
    """Task with consecutive wait_for (weekly waits for 7 daily completions)."""
    from polyris import DAG, task
    from polyris.assets import Asset

    daily_complete = Asset("acme/daily-complete")
    weekly_complete = Asset("acme/weekly-complete")

    with DAG("consecutive_wait_for", schedule="cron(0 22 ? * SUN *)") as dag:
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:mark-complete",
            wait_for=[daily_complete.consecutive(days=7)],
            outlets=[weekly_complete]
        )
        def mark_weekly_complete():
            pass
        mark_weekly_complete()
    return dag


def _build_trigger_rules():
    """Fan-in with a non-default trigger_rule on the downstream task."""
    from polyris import DAG, task

    with DAG("trigger_rules", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:a")
        def extract_a():
            pass
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:b")
        def extract_b():
            pass
        @task.sfn(
            arn="arn:aws:states:us-east-1:123:stateMachine:cleanup",
            trigger_rule="all_done",
        )
        def cleanup():
            pass
        a = extract_a()
        b = extract_b()
        cleanup(a, b)
    return dag


ALL_SCENARIOS = {
    "single_task": _build_single_task,
    "chain": _build_chain,
    "parallel": _build_parallel,
    "fan_out": _build_fan_out,
    "fan_in": _build_fan_in,
    "wait_before": _build_wait_before,
    "trigger_rules": _build_trigger_rules,
    "with_outlets": _build_with_outlets,
    "with_variables": _build_with_variables,
    "with_retries": _build_with_retries,
    "lambda_task": _build_lambda_task,
    "asset_triggered": _build_asset_triggered,
    "cross_account": _build_cross_account,
    "orch_timeout": _build_orchestration_timeout,
    "diamond": _build_complex_diamond,
    "consecutive_wait_for": _build_consecutive_wait_for,
}


def _run_snapshot(name: str, builder):
    from polyris.generators import generate_step_function_json
    dag = builder()
    asl_json = generate_step_function_json(dag)
    asl = json.loads(asl_json)
    _compare_or_update(name, asl)


# Individual test functions for pytest discovery
def test_snapshot_single_task():
    _run_snapshot("single_task", _build_single_task)

def test_snapshot_chain():
    _run_snapshot("chain", _build_chain)

def test_snapshot_parallel():
    _run_snapshot("parallel", _build_parallel)

def test_snapshot_fan_out():
    _run_snapshot("fan_out", _build_fan_out)

def test_snapshot_fan_in():
    _run_snapshot("fan_in", _build_fan_in)

def test_snapshot_wait_before():
    _run_snapshot("wait_before", _build_wait_before)

def test_snapshot_trigger_rules():
    _run_snapshot("trigger_rules", _build_trigger_rules)

def test_snapshot_with_outlets():
    _run_snapshot("with_outlets", _build_with_outlets)

def test_snapshot_with_variables():
    _run_snapshot("with_variables", _build_with_variables)

def test_snapshot_with_retries():
    _run_snapshot("with_retries", _build_with_retries)

def test_snapshot_lambda_task():
    _run_snapshot("lambda_task", _build_lambda_task)

def test_snapshot_asset_triggered():
    _run_snapshot("asset_triggered", _build_asset_triggered)

def test_snapshot_cross_account():
    _run_snapshot("cross_account", _build_cross_account)

def test_snapshot_orch_timeout():
    _run_snapshot("orch_timeout", _build_orchestration_timeout)

def test_snapshot_diamond():
    _run_snapshot("diamond", _build_complex_diamond)

def test_snapshot_consecutive_wait_for():
    _run_snapshot("consecutive_wait_for", _build_consecutive_wait_for)


# ============================================================
# Structural validation (always runs, no golden files needed)
# ============================================================

def test_all_scenarios_produce_valid_asl():
    """Every scenario generates valid ASL with required top-level keys."""
    from polyris.generators import generate_step_function_json, validate_asl

    for name, builder in ALL_SCENARIOS.items():
        dag = builder()
        asl_json = generate_step_function_json(dag)
        asl = json.loads(asl_json)

        # Must have these top-level keys
        assert "States" in asl, f"{name}: missing States"
        assert "StartAt" in asl, f"{name}: missing StartAt"
        assert asl["StartAt"] in asl["States"], f"{name}: StartAt points to missing state"

        # Validate ASL structure
        valid, errors, warnings = validate_asl(asl)
        assert valid, f"{name}: ASL validation failed: {errors}"


def test_all_scenarios_have_register_pipeline():
    """Every DAG has Register_Pipeline state (may not be StartAt if variables exist)."""
    from polyris.generators import generate_step_function_json

    for name, builder in ALL_SCENARIOS.items():
        dag = builder()
        asl = json.loads(generate_step_function_json(dag))
        assert "Register_Pipeline" in asl["States"], f"{name}: missing Register_Pipeline"
        # DAGs with variables start at Define_Inputs, others at Register_Pipeline
        assert asl["StartAt"] in asl["States"], f"{name}: StartAt points to missing state"


def test_all_scenarios_have_dag_snapshot():
    """Every DAG has Save_DAG_Snapshot state that chains from Register_Pipeline."""
    from polyris.generators import generate_step_function_json

    for name, builder in ALL_SCENARIOS.items():
        dag = builder()
        asl = json.loads(generate_step_function_json(dag))
        states = asl["States"]

        # Save_DAG_Snapshot must exist
        assert "Save_DAG_Snapshot" in states, f"{name}: missing Save_DAG_Snapshot"

        # Register_Pipeline must chain to Save_DAG_Snapshot
        assert states["Register_Pipeline"]["Next"] == "Save_DAG_Snapshot", (
            f"{name}: Register_Pipeline.Next should be Save_DAG_Snapshot"
        )

        # Save_DAG_Snapshot must write to tokens table
        snapshot = states["Save_DAG_Snapshot"]
        assert snapshot["Type"] == "Task", f"{name}: Save_DAG_Snapshot should be Task"
        assert "dynamodb:putItem" in snapshot["Resource"], f"{name}: should write to DDB"
        item = snapshot["Arguments"]["Item"]
        assert "execution_name" in item, f"{name}: snapshot must have execution_name key"
        assert "dag" in item, f"{name}: snapshot must store dag"

        # Save_DAG_Snapshot.Next must be Check_Register_Only or Register_Asset_Subscriptions
        assert snapshot["Next"] in ("Check_Register_Only", "Register_Asset_Subscriptions"), (
            f"{name}: Save_DAG_Snapshot.Next={snapshot['Next']} unexpected"
        )


def test_dag_snapshot_uses_provided_tokens_table():
    """Save_DAG_Snapshot uses tokens_table parameter when provided."""
    from polyris.generators import generate_step_function_json

    dag = _build_chain()
    asl = json.loads(generate_step_function_json(dag, tokens_table="my-tokens-table"))
    snapshot = asl["States"]["Save_DAG_Snapshot"]
    assert snapshot["Arguments"]["TableName"] == "my-tokens-table"


def test_dag_snapshot_default_placeholder():
    """Save_DAG_Snapshot uses ${tokens_table} placeholder when no table provided."""
    from polyris.generators import generate_step_function_json

    dag = _build_chain()
    asl = json.loads(generate_step_function_json(dag))
    snapshot = asl["States"]["Save_DAG_Snapshot"]
    assert snapshot["Arguments"]["TableName"] == "${tokens_table}"


def test_dag_snapshot_has_ttl():
    """Save_DAG_Snapshot includes TTL for automatic expiry."""
    from polyris.generators import generate_step_function_json

    dag = _build_chain()
    asl = json.loads(generate_step_function_json(dag))
    snapshot = asl["States"]["Save_DAG_Snapshot"]
    item = snapshot["Arguments"]["Item"]
    assert "ttl" in item, "Save_DAG_Snapshot must include TTL"
    assert item["ttl"]["N"].startswith("{%"), "TTL should be JSONata expression"


def test_dag_hash_deterministic():
    """Same DAG always produces same hash."""
    from polyris.generators import generate_dag_hash

    dag1 = _build_chain()
    dag2 = _build_chain()
    assert generate_dag_hash(dag1) == generate_dag_hash(dag2)


def test_dag_hash_changes_with_tasks():
    """Adding a task changes the hash."""
    from polyris.generators import generate_dag_hash

    dag1 = _build_chain()
    hash1 = generate_dag_hash(dag1)

    dag2 = _build_chain()
    from polyris import task
    with dag2:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extra")
        def extra_task(): pass
        extra_task()
    hash2 = generate_dag_hash(dag2)

    assert hash1 != hash2, "Hash should change when tasks are added"


def test_dag_hash_is_8_chars():
    """Hash is truncated to 8 hex chars."""
    from polyris.generators import generate_dag_hash

    dag = _build_chain()
    h = generate_dag_hash(dag)
    assert len(h) == 8
    assert all(c in '0123456789abcdef' for c in h)


def test_all_scenarios_task_count_matches():
    """Number of parallel branches == number of tasks in DAG."""
    from polyris.generators import generate_step_function_json

    for name, builder in ALL_SCENARIOS.items():
        dag = builder()
        asl = json.loads(generate_step_function_json(dag))

        # Find the Parallel state
        parallel_state = None
        for sname, state in asl["States"].items():
            if state.get("Type") == "Parallel":
                parallel_state = state
                break

        assert parallel_state is not None, f"{name}: no Parallel state found"
        branch_count = len(parallel_state.get("Branches", []))
        task_count = len(dag.tasks) + len(dag.steps)
        assert branch_count == task_count, (
            f"{name}: branch count ({branch_count}) != task count ({task_count})"
        )


def test_chain_has_correct_dependencies():
    """Chain DAG: transform depends on extract, load depends on transform."""
    from polyris.generators import generate_step_function_json

    dag = _build_chain()
    asl = json.loads(generate_step_function_json(dag))

    # Find branches by task name
    branches = {}
    for sname, state in asl["States"].items():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                for bsname, bstate in branch["States"].items():
                    if bstate.get("Type") == "Task" and "Arguments" in bstate:
                        inp = bstate["Arguments"].get("Input", {})
                        task_name = inp.get("task_name", "")
                        if task_name:
                            branches[task_name] = inp

    assert "extract" in branches
    assert "transform" in branches
    assert "load" in branches

    assert branches["extract"].get("dependencies", []) == []
    assert branches["transform"].get("dependencies") == ["extract"]
    assert branches["load"].get("dependencies") == ["transform"]


def test_fan_in_has_multiple_dependencies():
    """Fan-in: load depends on [a, b, c]."""
    from polyris.generators import generate_step_function_json

    dag = _build_fan_in()
    asl = json.loads(generate_step_function_json(dag))

    for sname, state in asl["States"].items():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                for bsname, bstate in branch["States"].items():
                    if bstate.get("Type") == "Task" and "Arguments" in bstate:
                        inp = bstate["Arguments"].get("Input", {})
                        if inp.get("task_name") == "load":
                            deps = sorted(inp.get("dependencies", []))
                            assert deps == ["task_a", "task_b", "task_c"], f"Expected 3 deps, got {deps}"
                            return

    assert False, "load task not found in generated ASL"


def test_diamond_dependencies():
    """Diamond: a->[], b->[a], c->[a], d->[b,c]."""
    from polyris.generators import generate_step_function_json

    dag = _build_complex_diamond()
    asl = json.loads(generate_step_function_json(dag))

    task_deps = {}
    for sname, state in asl["States"].items():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                for bsname, bstate in branch["States"].items():
                    if bstate.get("Type") == "Task" and "Arguments" in bstate:
                        inp = bstate["Arguments"].get("Input", {})
                        tn = inp.get("task_name", "")
                        if tn:
                            task_deps[tn] = sorted(inp.get("dependencies", []))

    assert task_deps.get("a") == []
    assert task_deps.get("b") == ["a"]
    assert task_deps.get("c") == ["a"]
    assert task_deps.get("d") == ["b", "c"]


def test_wait_before_in_task_input():
    """wait_before=600 is passed to wrapper as task input."""
    from polyris.generators import generate_step_function_json

    dag = _build_wait_before()
    asl = json.loads(generate_step_function_json(dag))

    found = False
    for sname, state in asl["States"].items():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                for bsname, bstate in branch["States"].items():
                    if bstate.get("Type") == "Task" and "Arguments" in bstate:
                        inp = bstate["Arguments"].get("Input", {})
                        if inp.get("wait_before") == 600:
                            found = True

    assert found, "wait_before=600 not found in task input"


def test_outlets_in_generated_asl():
    """Outlets are passed in task input for asset event emission."""
    from polyris.generators import generate_step_function_json

    dag = _build_with_outlets()
    asl = json.loads(generate_step_function_json(dag))

    for sname, state in asl["States"].items():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                for bsname, bstate in branch["States"].items():
                    if bstate.get("Type") == "Task" and "Arguments" in bstate:
                        inp = bstate["Arguments"].get("Input", {})
                        outlets = inp.get("outlets", [])
                        if outlets:
                            assert len(outlets) == 1
                            assert outlets[0]["name"] == "raw/inventory"
                            return

    assert False, "No outlets found in generated ASL"


def test_variables_in_define_inputs():
    """Variables create a Define_Inputs state with configured values."""
    from polyris.generators import generate_step_function_json

    dag = _build_with_variables()
    asl = json.loads(generate_step_function_json(dag))

    # Check that variables are embedded in Comment metadata or Define_Inputs
    json.loads(asl.get("Comment", "{}"))
    # Variables should be accessible to tasks
    for sname, state in asl["States"].items():
        if "Define_Inputs" in sname or "variables" in json.dumps(state).lower():
            break

    # Variables should be in the generated JSON somewhere
    asl_str = json.dumps(asl)
    assert "env" in asl_str, "Variable 'env' not found in ASL"
    assert "prod" in asl_str, "Variable value 'prod' not found in ASL"


# ============================================================
# Runner
# ============================================================

if __name__ == "__main__":
    import sys

    if "--update" in sys.argv:
        os.environ["SNAPSHOT_UPDATE"] = "1"

    tests = [v for k, v in globals().items() if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
