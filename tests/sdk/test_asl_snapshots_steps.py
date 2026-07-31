"""
ASL Snapshot Tests for Step branches in generators.py

Covers _build_step_branch() and _generate_step_state() which had
ZERO snapshot coverage prior to this file. Every Step type that
goes through generate_step_function_json() is tested here.

Coverage gap addressed:
- Direct steps (wait/pass/sns/sqs/s3/eventbridge/bedrock/http): run inline, no wrapper
- Wrapper steps via Step API (LambdaTask/GlueTask/ECSTask/AthenaTask): go through wrapper
- Mixed DAGs: Tasks + Steps in the same pipeline
- Validation: dependency restrictions on direct vs wrapper steps

Usage:
    pytest tests/sdk/test_asl_snapshots_steps.py
    SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots_steps.py
"""

import json
import os
from pathlib import Path

import pytest

SNAPSHOT_DIR = Path(__file__).parent.parent / "snapshots"
SNAPSHOT_DIR.mkdir(exist_ok=True)

UPDATE = os.environ.get("SNAPSHOT_UPDATE", "").strip() in ("1", "true", "yes")


def _compare_or_update(name: str, generated: dict):
    """Compare ASL against snapshot or update if SNAPSHOT_UPDATE=1."""
    path = SNAPSHOT_DIR / f"{name}.json"

    if UPDATE:
        path.write_text(json.dumps(generated, indent=2) + "\n")
        return

    assert path.exists(), (
        f"Snapshot {path.name} not found. "
        f"Run SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots_steps.py to generate."
    )
    expected = json.loads(path.read_text())
    assert generated == expected, (
        f"ASL mismatch for {name}. "
        f"Run SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots_steps.py to update.\n"
        f"Diff hint: check States keys: "
        f"got={sorted(generated.get('States', {}).keys())} "
        f"expected={sorted(expected.get('States', {}).keys())}"
    )


def _run_snapshot(name: str, builder):
    from polyris.generators import generate_step_function_json
    dag = builder()
    asl_json = generate_step_function_json(dag)
    asl = json.loads(asl_json)
    _compare_or_update(name, asl)
    return asl


# ============================================================
# DAG builders — Step scenarios
# ============================================================

def _build_step_direct_wait():
    """Task + Wait direct step — simplest mixed DAG."""
    from polyris import DAG, task, Wait

    with DAG("step_direct_wait", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass

        Wait(step_id="cooldown", seconds=30)

        extract()
    return dag


def _build_step_direct_pass():
    """Pass step with output transformation."""
    from polyris import DAG, task, Pass

    with DAG("step_direct_pass", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        Pass(
            step_id="prepare_params",
            output={
                "s3_path": "{% 's3://bucket/' & $states.input.current_date & '/' %}",
                "mode": "full"
            }
        )

        process()
    return dag


def _build_step_direct_sns_sqs():
    """SNS + SQS direct steps — service integration coverage."""
    from polyris import DAG, task, SNSTask, SQSTask

    with DAG("step_direct_sns_sqs", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        SNSTask(
            step_id="notify_team",
            topic_arn="arn:aws:sns:us-east-1:123456789:pipeline-alerts",
            message="Pipeline completed!",
            subject="Alert"
        )

        SQSTask(
            step_id="enqueue_next",
            queue_url="https://sqs.us-east-1.amazonaws.com/123456789/jobs",
            message_body="{% $string($states.input) %}",
            delay_seconds=10,
        )

        process()
    return dag


def _build_step_direct_s3():
    """S3 get + put operations — tests both operation types."""
    from polyris import DAG, task, S3Task

    with DAG("step_direct_s3", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:transform")
        def transform():
            pass

        S3Task(
            step_id="get_config",
            operation="get_object",
            bucket="data-bucket",
            key="config/pipeline.json"
        )

        S3Task(
            step_id="save_result",
            operation="put_object",
            bucket="data-bucket",
            key="output/result.json",
            body="{% $string($states.input) %}",
            content_type="application/json"
        )

        transform()
    return dag


def _build_step_direct_eventbridge():
    """EventBridge + Bedrock direct steps."""
    from polyris import DAG, task, EventBridgeTask, BedrockTask

    with DAG("step_direct_eventbridge", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        EventBridgeTask(
            step_id="emit_completion",
            event_bus="default",
            source="my.pipeline",
            detail_type="PipelineCompleted",
            detail={"pipeline": "step_direct_eventbridge", "status": "success"}
        )

        BedrockTask(
            step_id="ai_validate",
            model_id="anthropic.claude-3-sonnet",
            body={"prompt": "Validate this data", "max_tokens": 100}
        )

        process()
    return dag


def _build_step_direct_http():
    """HTTP task step — API call integration."""
    from polyris import DAG, task, HttpTask

    with DAG("step_direct_http", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        HttpTask(
            step_id="call_webhook",
            url="https://api.example.com/webhook",
            method="POST",
            headers={"Content-Type": "application/json"},
            body={"event": "pipeline_done"}
        )

        process()
    return dag


def _build_step_direct_dynamodb():
    """DynamoDB operations — put_item and query."""
    from polyris import DAG, task, DynamoDBTask

    with DAG("step_direct_dynamodb", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        DynamoDBTask(
            step_id="write_status",
            operation="put_item",
            table_name="pipeline-status",
            item={
                "pipeline_name": {"S": "step_direct_dynamodb"},
                "status": {"S": "completed"}
            }
        )

        DynamoDBTask(
            step_id="query_history",
            operation="query",
            table_name="pipeline-status",
            key_condition="pipeline_name = :name",
            expression_attribute_values={":name": {"S": "step_direct_dynamodb"}},
            index_name="status-index"
        )

        process()
    return dag


def _build_step_wrapper_lambda():
    """LambdaTask through wrapper — Step API (not @task decorator)."""
    from polyris import DAG, task, LambdaTask

    with DAG("step_wrapper_lambda", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass

        validate = LambdaTask(
            step_id="validate_data",
            function_arn="arn:aws:lambda:us-east-1:123:function:validate",
            payload={"mode": "strict"}
        )

        e = extract()
        e >> validate  # validate depends on extract
    return dag


def _build_step_wrapper_glue():
    """GlueTask through wrapper with dependencies on Task."""
    from polyris import DAG, task, GlueTask

    with DAG("step_wrapper_glue", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:prepare")
        def prepare():
            pass

        etl = GlueTask(
            step_id="run_etl",
            job_name="daily-etl-job",
            arguments={
                "--source_path": "s3://bucket/input/",
                "--target_path": "s3://bucket/output/"
            }
        )

        p = prepare()
        p >> etl  # etl depends on prepare
    return dag


def _build_step_wrapper_ecs_athena():
    """ECS + Athena through wrapper — both with deps on Task."""
    from polyris import DAG, task, ECSTask, AthenaTask

    with DAG("step_wrapper_ecs_athena", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass

        container = ECSTask(
            step_id="run_container",
            cluster="prod-cluster",
            task_definition="transform-task:3",
            launch_type="FARGATE",
            subnets=["subnet-abc123"],
            security_groups=["sg-xyz789"],
            overrides={
                "containerOverrides": [{
                    "name": "main",
                    "environment": [{"name": "MODE", "value": "prod"}]
                }]
            }
        )

        query = AthenaTask(
            step_id="run_report",
            query_string="SELECT count(*) FROM results",
            database="analytics",
            output_location="s3://bucket/athena-results/",
            workgroup="primary"
        )

        e = extract()
        e >> container
        e >> query  # both depend on extract
    return dag


def _build_step_mixed_full():
    """Full mixed DAG: @task + direct steps + wrapper steps.

    Pipeline structure:
      extract (task) → transform (task) → load (task)
      wait_30s (direct, no deps)
      notify_sns (direct, no deps)
      validate_lambda (wrapper, depends on extract)
    """
    from polyris import DAG, task, Wait, SNSTask, LambdaTask

    with DAG("step_mixed_full", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass

        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:transform")
        def transform():
            pass

        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:load")
        def load():
            pass

        Wait(step_id="cooldown", seconds=30)

        SNSTask(
            step_id="notify_start",
            topic_arn="arn:aws:sns:us-east-1:123:alerts",
            message="Pipeline started"
        )

        validate = LambdaTask(
            step_id="validate",
            function_arn="arn:aws:lambda:us-east-1:123:function:validate",
            payload={"strict": True}
        )

        e = extract()
        t = transform(e)
        load(t)
        e >> validate  # wrapper step depends on task
    return dag


# ============================================================
# Scenario registry
# ============================================================

STEP_SCENARIOS = {
    "step_direct_wait": _build_step_direct_wait,
    "step_direct_pass": _build_step_direct_pass,
    "step_direct_sns_sqs": _build_step_direct_sns_sqs,
    "step_direct_s3": _build_step_direct_s3,
    "step_direct_eventbridge": _build_step_direct_eventbridge,
    "step_direct_http": _build_step_direct_http,
    "step_direct_dynamodb": _build_step_direct_dynamodb,
    "step_wrapper_lambda": _build_step_wrapper_lambda,
    "step_wrapper_glue": _build_step_wrapper_glue,
    "step_wrapper_ecs_athena": _build_step_wrapper_ecs_athena,
    "step_mixed_full": _build_step_mixed_full,
}


# ============================================================
# Snapshot tests — golden file comparisons
# ============================================================

def test_snapshot_step_direct_wait():
    _run_snapshot("step_direct_wait", _build_step_direct_wait)

def test_snapshot_step_direct_pass():
    _run_snapshot("step_direct_pass", _build_step_direct_pass)

def test_snapshot_step_direct_sns_sqs():
    _run_snapshot("step_direct_sns_sqs", _build_step_direct_sns_sqs)

def test_snapshot_step_direct_s3():
    _run_snapshot("step_direct_s3", _build_step_direct_s3)

def test_snapshot_step_direct_eventbridge():
    _run_snapshot("step_direct_eventbridge", _build_step_direct_eventbridge)

def test_snapshot_step_direct_http():
    _run_snapshot("step_direct_http", _build_step_direct_http)

def test_snapshot_step_direct_dynamodb():
    _run_snapshot("step_direct_dynamodb", _build_step_direct_dynamodb)

def test_snapshot_step_wrapper_lambda():
    _run_snapshot("step_wrapper_lambda", _build_step_wrapper_lambda)

def test_snapshot_step_wrapper_glue():
    _run_snapshot("step_wrapper_glue", _build_step_wrapper_glue)

def test_snapshot_step_wrapper_ecs_athena():
    _run_snapshot("step_wrapper_ecs_athena", _build_step_wrapper_ecs_athena)

def test_snapshot_step_mixed_full():
    _run_snapshot("step_mixed_full", _build_step_mixed_full)


# ============================================================
# Structural validation — no golden files needed
# ============================================================

def test_all_step_scenarios_produce_valid_asl():
    """Every Step scenario generates valid ASL."""
    from polyris.generators import generate_step_function_json, validate_asl

    for name, builder in STEP_SCENARIOS.items():
        dag = builder()
        asl = json.loads(generate_step_function_json(dag))

        assert "States" in asl, f"{name}: missing States"
        assert "StartAt" in asl, f"{name}: missing StartAt"
        assert asl["StartAt"] in asl["States"], f"{name}: StartAt → missing state"

        valid, errors, warnings = validate_asl(asl)
        assert valid, f"{name}: ASL validation failed: {errors}"


def test_all_step_scenarios_branch_count():
    """Branch count == tasks + steps."""
    from polyris.generators import generate_step_function_json

    for name, builder in STEP_SCENARIOS.items():
        dag = builder()
        asl = json.loads(generate_step_function_json(dag))

        parallel = None
        for state in asl["States"].values():
            if state.get("Type") == "Parallel":
                parallel = state
                break

        assert parallel is not None, f"{name}: no Parallel state"
        branches = len(parallel["Branches"])
        expected = len(dag.tasks) + len(dag.steps)
        assert branches == expected, (
            f"{name}: {branches} branches != {expected} (tasks={len(dag.tasks)}, steps={len(dag.steps)})"
        )


def test_direct_steps_run_inline():
    """Direct steps (wait/pass/sns/sqs/s3) produce inline ASL, NOT wrapper calls."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_wait()
    asl = json.loads(generate_step_function_json(dag))

    # Find the Wait step branch
    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "cooldown" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert step_state["Type"] == "Wait", "Wait step should be inline Wait type"
                    assert step_state.get("Seconds") == 30
                    # Must NOT go through wrapper (no startExecution resource)
                    assert "startExecution" not in step_state.get("Resource", "")
                    return

    pytest.fail("cooldown Wait step not found in branches")


def test_wrapper_steps_use_wrapper():
    """Wrapper steps (lambda/glue/ecs/athena via Step API) go through wrapper SFN."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_wrapper_lambda()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "validate_data" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert step_state["Type"] == "Task"
                    assert "startExecution.waitForTaskToken" in step_state["Resource"]
                    inp = step_state["Arguments"]["Input"]
                    assert inp["task_name"] == "validate_data"
                    assert inp["task_type"] == "lambda"
                    assert inp["task_config"]["payload"] == {"mode": "strict"}
                    return

    pytest.fail("validate_data wrapper step not found")


def test_wrapper_step_has_dependencies():
    """Wrapper steps carry dependency info to the wrapper."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_wrapper_lambda()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "validate_data" in branch["StartAt"]:
                    inp = branch["States"][branch["StartAt"]]["Arguments"]["Input"]
                    assert inp["dependencies"] == ["extract"], (
                        f"Expected ['extract'], got {inp['dependencies']}"
                    )
                    return

    pytest.fail("validate_data not found")


def test_wrapper_step_glue_has_task_config():
    """GlueTask wrapper step carries job_name and arguments in task_config."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_wrapper_glue()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "run_etl" in branch["StartAt"]:
                    inp = branch["States"][branch["StartAt"]]["Arguments"]["Input"]
                    assert inp["task_type"] == "glue"
                    tc = inp["task_config"]
                    assert tc["job_name"] == "daily-etl-job"
                    assert "--source_path" in tc["arguments"]
                    return

    pytest.fail("run_etl not found")


def test_wrapper_step_ecs_has_network_config():
    """ECS wrapper step carries cluster, task_definition, subnets."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_wrapper_ecs_athena()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "run_container" in branch["StartAt"]:
                    inp = branch["States"][branch["StartAt"]]["Arguments"]["Input"]
                    assert inp["task_type"] == "ecs"
                    tc = inp["task_config"]
                    assert tc["cluster"] == "prod-cluster"
                    assert tc["subnets"] == ["subnet-abc123"]
                    assert tc["security_groups"] == ["sg-xyz789"]
                    return

    pytest.fail("run_container not found")


def test_sns_step_produces_correct_resource():
    """SNS direct step uses sns:publish resource."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_sns_sqs()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "notify_team" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert "sns:publish" in step_state["Resource"]
                    assert step_state["Arguments"]["TopicArn"] == \
                        "arn:aws:sns:us-east-1:123456789:pipeline-alerts"
                    assert step_state["Arguments"]["Subject"] == "Alert"
                    return

    pytest.fail("notify_team SNS step not found")


def test_sqs_step_has_delay():
    """SQS step carries delay_seconds."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_sns_sqs()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "enqueue_next" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert "sqs:sendMessage" in step_state["Resource"]
                    assert step_state["Arguments"]["DelaySeconds"] == 10
                    return

    pytest.fail("enqueue_next SQS step not found")


def test_s3_put_has_body_and_content_type():
    """S3 put_object step carries body and content_type."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_s3()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "save_result" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert "s3:putObject" in step_state["Resource"]
                    args = step_state["Arguments"]
                    assert args["Bucket"] == "data-bucket"
                    assert args["ContentType"] == "application/json"
                    assert "Body" in args
                    return

    pytest.fail("save_result S3 step not found")


def test_dynamodb_query_has_index():
    """DynamoDB query step carries KeyConditionExpression and IndexName."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_dynamodb()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "query_history" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert "dynamodb:query" in step_state["Resource"]
                    args = step_state["Arguments"]
                    assert args["IndexName"] == "status-index"
                    assert args["KeyConditionExpression"] == "pipeline_name = :name"
                    return

    pytest.fail("query_history DynamoDB step not found")


def test_http_step_has_headers():
    """HTTP step carries method, headers, body."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_direct_http()
    asl = json.loads(generate_step_function_json(dag))

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                if "call_webhook" in branch["StartAt"]:
                    step_state = branch["States"][branch["StartAt"]]
                    assert "http:invoke" in step_state["Resource"]
                    args = step_state["Arguments"]
                    assert args["Method"] == "POST"
                    assert args["Headers"]["Content-Type"] == "application/json"
                    assert args["RequestBody"] == {"event": "pipeline_done"}
                    return

    pytest.fail("call_webhook HTTP step not found")


def test_mixed_dag_has_all_branches():
    """Mixed DAG has both task and step branches with correct types."""
    from polyris.generators import generate_step_function_json

    dag = _build_step_mixed_full()
    asl = json.loads(generate_step_function_json(dag))

    branch_names = set()
    wrapper_branches = set()
    direct_branches = set()

    for state in asl["States"].values():
        if state.get("Type") == "Parallel":
            for branch in state["Branches"]:
                start = branch["StartAt"]
                branch_names.add(start)
                step_state = branch["States"][start]
                resource = step_state.get("Resource", "")
                if "startExecution.waitForTaskToken" in resource:
                    wrapper_branches.add(start)
                elif step_state["Type"] in ("Wait", "Pass"):
                    direct_branches.add(start)
                elif "sns:publish" in resource:
                    direct_branches.add(start)

    # 3 tasks go through wrapper
    assert "Task_extract" in wrapper_branches
    assert "Task_transform" in wrapper_branches
    assert "Task_load" in wrapper_branches

    # 1 LambdaTask wrapper step
    assert "validate" in wrapper_branches

    # 2 direct steps
    assert "cooldown" in direct_branches
    assert "notify_start" in direct_branches


# ============================================================
# Validation tests — error cases
# ============================================================

def test_direct_step_with_deps_raises():
    """Direct step (Wait) cannot have dependencies — must raise ValueError."""
    from polyris import DAG, task, Wait
    from polyris.generators import generate_step_function_json

    with DAG("invalid_direct_deps", schedule=None) as dag:
        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:extract")
        def extract():
            pass

        wait = Wait(step_id="bad_wait", seconds=10)
        e = extract()
        e >> wait  # Direct step depending on task — not allowed

    with pytest.raises(ValueError, match="cannot have dependencies"):
        generate_step_function_json(dag)


def test_wrapper_step_depending_on_direct_step_raises():
    """Wrapper step (LambdaTask) cannot depend on direct step (Wait)."""
    from polyris import DAG, Wait, LambdaTask
    from polyris.generators import generate_step_function_json

    with DAG("invalid_wrapper_on_direct", schedule=None) as dag:
        wait = Wait(step_id="my_wait", seconds=10)

        validate = LambdaTask(
            step_id="validate",
            function_arn="arn:aws:lambda:us-east-1:123:function:validate",
        )

        validate << wait  # Wrapper step depends on direct step — not allowed

    with pytest.raises(ValueError, match="depends on direct step"):
        generate_step_function_json(dag)


def test_task_depending_on_direct_step_raises():
    """@task cannot depend on direct step (SNS)."""
    from polyris import DAG, task, SNSTask
    from polyris.generators import generate_step_function_json

    with DAG("invalid_task_on_direct", schedule=None) as dag:
        notify = SNSTask(
            step_id="notify",
            topic_arn="arn:aws:sns:us-east-1:123:alerts",
            message="hello"
        )

        @task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:process")
        def process():
            pass

        p = process()
        # Task depending on direct step
        p.task.dependencies.append(notify)

    with pytest.raises(ValueError, match="depends on direct step"):
        generate_step_function_json(dag)


# ============================================================
# Runner
# ============================================================

if __name__ == "__main__":
    import sys as _sys

    if "--update" in _sys.argv:
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
    _sys.exit(1 if failed else 0)
