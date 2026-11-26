"""
Contract tests for the shared run_task helper SFN template.

This helper is invoked **per task** by every user pipeline ASL. The
external reviewer correctly identified that several backfill options
were silently ignored by the child template because the API plumbed
them through bulk_backfill SFN but run_task didn't read them.

These tests pin the contract so future edits can't silently strip the
skip_tasks Choice or the _suppress_asset_event guard. They are the
sibling of tests/sdk/test_bulk_backfill_template.py.
"""

import json
import re
from pathlib import Path

import pytest

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"


@pytest.fixture(scope="module")
def template() -> dict:
    """Load run_task template (with SAM placeholders normalized for JSON parse)."""
    raw = TEMPLATE_PATH.read_text()
    clean = re.sub(r'\$\{[^}]+\}', '0', raw)
    return json.loads(clean)


def test_jsonata_mode(template):
    """All expression-based contracts below assume JSONata mode."""
    assert template.get("QueryLanguage") == "JSONata"


RUN_ARN = "arn:aws:states:us-east-1:123456789012:execution:acme-dev-polyris-run-task-helper:run-abc"


def _context(run_input):
    return {"Execution": {"Input": run_input, "Id": RUN_ARN, "Name": "run-abc"}}


def _catches(state):
    return {error: c["Next"] for c in state.get("Catch", []) for error in c["ErrorEquals"]}


def test_the_first_state_claims_the_row_under_its_generation(template):
    """deps_ready -> running under the generation the dispatcher started it with (ADR #137 §1, invariant 5)."""
    from polyris.coordination import task_row
    assert template["StartAt"] == "Claim"
    arguments = template["States"]["Claim"]["Arguments"]
    assert arguments["ConditionExpression"] == f"#s = :deps_ready AND {task_row.FENCE}"
    assert arguments["ExpressionAttributeNames"]["#g"] == task_row.GENERATION
    values = arguments["ExpressionAttributeValues"]
    assert (values[":deps_ready"], values[":running"]) == ({"S": "deps_ready"}, {"S": "running"})
    assert "attempt = if_not_exists(attempt, :zero) + :one" in arguments["UpdateExpression"]
    assert arguments["ReturnValues"] == "UPDATED_NEW"


def test_the_claim_keeps_the_liveness_check_the_start_put_on_the_row(template):
    """The start made the row due at its first liveness check, counted from status_since; a claim that wrote
    status_since would leave that due time stale, and a pass would rewrite every started row once."""
    expression = template["States"]["Claim"]["Arguments"]["UpdateExpression"]
    assert "status_since" not in expression and "attention" not in expression


def test_a_claim_whose_answer_was_lost_goes_on_and_another_owners_ends_quietly(template):
    states = template["States"]
    assert _catches(states["Claim"])["DynamoDB.ConditionalCheckFailedException"] == "Read_Claim"
    run_input = {"generation": 3, "execution_name": "load-2026-11-18-x"}

    def output(item):
        jsonata = pytest.importorskip("jsonata")
        expression = jsonata.Jsonata(states["Read_Claim"]["Output"][2:-2].strip())
        expression.assign("states", {"input": {}, "result": {"Item": item} if item is not None else {},
                                     "context": _context(run_input)})
        return expression.evaluate(None)["claimed"]

    mine = {"status": {"S": "running"}, "generation": {"N": "3"}, "run_task_helper_arn": {"S": RUN_ARN}}
    assert output(mine) is True
    assert output({**mine, "generation": {"N": "4"}}) is False                # a raised generation (Rule 47)
    assert output({**mine, "run_task_helper_arn": {"S": RUN_ARN + "-2"}}) is False
    assert output({**mine, "status": {"S": "stopped"}}) is False
    assert output(None) is False                                               # the row is gone
    route = states["Claimed_Before"]
    assert route["Choices"][0]["Next"] == "Emit_Task_Started" and route["Default"] == "Superseded"


def test_a_claim_that_cannot_be_written_fails_the_execution_for_the_dispatcher_to_judge(template):
    """The row stays deps_ready; the dispatcher fails it with the execution's error at its liveness check."""
    states = template["States"]
    assert _catches(states["Claim"])["States.ALL"] == "Claim_Failed"
    assert _catches(states["Read_Claim"])["States.ALL"] == "Claim_Failed"
    assert states["Claim_Failed"]["Type"] == "Fail"


def test_run_task_only_runs_the_work(template):
    """It no longer skips, checks the pause, waits for a decision, notifies dependants or answers the run (§1)."""
    text = TEMPLATE_PATH.read_text()
    for gone in ("skip_tasks", "pause_waiter_arn", "notify_dependents_helper_arn", "sendTaskSuccess",
                 "sendTaskFailure", "waitForTaskToken", "orchestration_token"):
        assert gone not in text, gone
    assert not [n for n, s in template["States"].items() if s["Type"] == "Wait" and n != "Wait_Before_Retry"
                and n != "Wait_Before_Recording_Again"]


def test_no_orphan_states(template):
    """All declared states must be reachable from StartAt. Catches typos
    in Next/Default that would create unreachable code."""
    states = template["States"]
    reachable = set()

    def walk(state_name):
        if state_name in reachable or state_name not in states:
            return
        reachable.add(state_name)
        s = states[state_name]
        # Linear next
        for k in ("Next", "Default"):
            if k in s and isinstance(s[k], str):
                walk(s[k])
        # Choice branches
        for c in s.get("Choices", []):
            if isinstance(c.get("Next"), str):
                walk(c["Next"])
        # Catch
        for c in s.get("Catch", []):
            if isinstance(c.get("Next"), str):
                walk(c["Next"])
        # Map nested
        if s.get("ItemProcessor", {}).get("States"):
            # Sub-states are independent — just verify they're walkable too
            sub_states = s["ItemProcessor"]["States"]
            sub_start = s["ItemProcessor"]["StartAt"]
            sub_reachable = set()
            def walk_sub(name):
                if name in sub_reachable or name not in sub_states:
                    return
                sub_reachable.add(name)
                ss = sub_states[name]
                for k in ("Next", "Default"):
                    if k in ss and isinstance(ss[k], str):
                        walk_sub(ss[k])
                for c in ss.get("Choices", []):
                    if isinstance(c.get("Next"), str):
                        walk_sub(c["Next"])
            for name, ss in sub_states.items():
                for c in ss.get("Catch", []):
                    if name in sub_reachable or name == sub_start:
                        sub_reachable.add(c["Next"])
            walk_sub(sub_start)
            unreachable_sub = set(sub_states.keys()) - sub_reachable
            assert not unreachable_sub, (
                f"Map sub-states unreachable in {state_name}: {unreachable_sub}"
            )

    walk(template["StartAt"])
    orphans = set(states.keys()) - reachable
    assert not orphans, f"Unreachable top-level states: {orphans}"


# ---------------------------------------------------------------------------
# task_config <-> Run_Task_<X> contract (Principle #13)
#
# The pipeline ASL passes a per-type ``task_config`` to run_task; each
# ``Run_Task_<X>`` reads it. These tests evaluate that wrapper Arguments JSONata
# against the EXACT ``task_config`` the SDK emits, so a mismatch (SDK writes key
# A, wrapper reads key B) fails here instead of at runtime.
#
# CRITICAL: jsonata-python does NOT bind ``$states`` from the evaluated root —
# it must be ``.assign()``-ed. Without that every ``$states.*`` silently
# resolves to undefined and the assertions pass on nothing. Each test therefore
# asserts a known field first as a binding guard.
# ---------------------------------------------------------------------------

def _wrapper_input_for(build, *, upstreams=None):
    """Build a one-task DAG via the real SDK and return run_task's input as the dispatcher builds it from
    the task's spec (run_task_input.build, ADR #137 §8, §9)."""
    from polyris import DAG
    from polyris.coordination import run_task_input, task_spec
    from polyris.generators import build_task_specs
    with DAG(dag_id="contract", schedule="@daily") as dag:
        build(dag)
    text = task_spec.canonical_json(build_task_specs(dag)[0])
    spec = task_spec.parse(text, expected_hash=task_spec.text_hash(text))
    if upstreams:
        text = task_spec.canonical_json({**spec.fields, "dependencies": sorted(upstreams)})
        spec = task_spec.parse(text, expected_hash=task_spec.text_hash(text))
    return json.loads(run_task_input.build(
        execution_name=f"{spec.task_id}-2026-11-12-contract-run-x", generation=1, task_name=spec.task_id,
        pipeline_name=dag.dag_id, pipeline_execution="contract-run-2026-11-12-x", date="2026-11-12",
        partition_arg="", backfill_id=None, spec=spec, variables={}, cascade="auto", upstreams=upstreams or {}))


def _resolve_arguments(template, state_name, wrapper_input, context_execution_id=None):
    """Resolve a Run_Task_<X> state's Arguments JSONata with $states bound.

    Pass context_execution_id to bind $states.context.Execution.Id for expressions
    that read the wrapper's own run identifier (POLYRIS_WRAPPER_RUN_ID injection
    reads this — otherwise JSONata drops it as undefined and the key disappears).
    """
    jsonata = pytest.importorskip("jsonata")
    # run_task reads its input from the execution's context (run_task_input.Key) and what changes from its
    # state; one dict serves as both here.
    states_binding = {"input": wrapper_input, "context": {"Execution": {"Input": wrapper_input}}}
    if context_execution_id is not None:
        states_binding["context"]["Execution"]["Id"] = context_execution_id

    def resolve(node):
        if isinstance(node, str) and node.startswith("{%") and node.endswith("%}"):
            expr = node[2:-2].strip()
            j = jsonata.Jsonata(expr)
            j.assign("states", states_binding)
            return j.evaluate({})
        if isinstance(node, dict):
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(template["States"][state_name]["Arguments"])


def test_emr_step_reaches_addstep(template):
    """@task.emr_step's emr_step must arrive intact as the addStep ``Step`` argument.
    Regression for the task_config schema mismatch (SDK emitted {cluster_id,
    step}; the wrapper read flat step_name/jar/... -> HadoopJarStep.Jar empty)."""
    from polyris import task
    jar = "s3://bucket/spark.jar"

    def build(dag):
        @task.emr_step(
            emr_cluster_id="j-ABC",
            emr_step={
                "Name": "Spark",
                "ActionOnFailure": "CONTINUE",
                "HadoopJarStep": {"Jar": jar, "Args": ["--date", "2026-01-01"]},
            },
        )
        def step():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_EMR", wi)

    # binding guard: if this fails, $states never resolved and the rest is meaningless
    assert resolved["ClusterId"] == "j-ABC", "binding/contract broken: ClusterId unresolved"

    # the actual contract: the user's step (incl. the required Jar) must arrive
    assert resolved["Step"]["HadoopJarStep"]["Jar"] == jar, (
        f"emr_step did not reach addStep; resolved Step={resolved.get('Step')!r}"
    )


def test_emr_rejects_step_without_jar():
    """@task.emr_step must reject a step missing the required HadoopJarStep.Jar at
    decoration time, not silently emit an invalid addStep call."""
    from polyris import DAG, task
    with DAG(dag_id="bad-emr", schedule="@daily"):
        with pytest.raises(ValueError, match="HadoopJarStep.Jar"):
            @task.emr_step(emr_cluster_id="j-ABC", emr_step={"Name": "x", "HadoopJarStep": {}})
            def step():
                pass


def test_emr_rejects_plural_steps():
    """Reject the RunJobFlow-style plural 'Steps' — addStep.sync takes one step."""
    from polyris import DAG, task
    with DAG(dag_id="bad-emr2", schedule="@daily"):
        with pytest.raises(ValueError, match="single StepConfig"):
            @task.emr_step(emr_cluster_id="j-ABC",
                      emr_step={"Steps": [{"HadoopJarStep": {"Jar": "x"}}]})
            def step():
                pass


def test_emr_step_is_statically_expanded(template):
    """Deploy-time guard. Step Functions validates the elasticmapreduce:addStep
    schema statically at CREATE/UPDATE, so the required Step.Name and
    Step.HadoopJarStep.Jar must be *literal keys* in the template — not hidden
    inside an opaque '{% $states.input.task_config.step %}' expression, which
    deploys with SCHEMA_VALIDATION_FAILED (Step.Name / Step.HadoopJarStep.Jar
    'required but missing'). The runtime-resolution tests above pass either way,
    so this static check is what catches a regression to the opaque form."""
    step = template["States"]["Run_Task_EMR"]["Arguments"]["Step"]
    assert isinstance(step, dict), (
        "Run_Task_EMR Step must be a literal object so deploy-time schema "
        f"validation can see the required fields; got opaque {step!r}"
    )
    assert "Name" in step, "Step.Name must be a literal key (addStep schema requires it)"
    hjs = step.get("HadoopJarStep")
    assert isinstance(hjs, dict) and "Jar" in hjs, (
        "Step.HadoopJarStep.Jar must be a literal key (addStep schema requires it); "
        f"got HadoopJarStep={hjs!r}"
    )


# --- glue: worker sizing must reach startJobRun (G4) ---

def test_glue_sizing_reaches_startjobrun(template):
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl", worker_type="G.2X", number_of_workers=10,
                   glue_arguments={"--date": "2026-01-01"})
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl"  # binding guard
    assert resolved.get("WorkerType") == "G.2X", f"WorkerType dropped: {resolved!r}"
    assert resolved.get("NumberOfWorkers") == 10, f"NumberOfWorkers dropped: {resolved!r}"


def test_glue_omits_sizing_when_unset(template):
    """A glue task without sizing must NOT emit empty WorkerType keys."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl")
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl"
    assert "WorkerType" not in resolved
    assert "NumberOfWorkers" not in resolved


def test_glue_rejects_allocated_and_worker_together():
    from polyris import DAG, task
    with DAG(dag_id="bad-glue", schedule="@daily"):
        with pytest.raises(ValueError, match="mutually exclusive"):
            @task.glue_job(job_name="x", worker_type="G.1X", number_of_workers=2,
                       allocated_capacity=5)
            def j():
                pass


def test_glue_rejects_worker_type_without_count():
    from polyris import DAG, task
    with DAG(dag_id="bad-glue2", schedule="@daily"):
        with pytest.raises(ValueError, match="together"):
            @task.glue_job(job_name="x", worker_type="G.1X")
            def j():
                pass


# --- ecs: assign_public_ip must reach runTask; NetworkConfiguration conditional (G5) ---

def test_ecs_assign_public_ip_reaches_runtask(template):
    from polyris import task

    def build(dag):
        @task.ecs_task(cluster="c", task_definition="td:1", subnets=["subnet-1"],
                  assign_public_ip="ENABLED")
        def e():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_ECS", wi)
    assert resolved["Cluster"] == "c"  # binding guard
    net = resolved["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert net["AssignPublicIp"] == "ENABLED", f"assign_public_ip dropped: {net!r}"
    assert net["Subnets"] == ["subnet-1"]


def test_ecs_ec2_without_subnets_omits_network_config(template):
    """EC2 + bridge/host (no subnets) must NOT emit NetworkConfiguration —
    runTask rejects AwsvpcConfiguration for non-awsvpc task defs."""
    from polyris import task

    def build(dag):
        @task.ecs_task(cluster="c", task_definition="td:1", launch_type="EC2")
        def e():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_ECS", wi)
    assert resolved["Cluster"] == "c"
    assert "NetworkConfiguration" not in resolved, f"NetworkConfiguration leaked: {resolved!r}"


def test_ecs_fargate_requires_subnets():
    from polyris import DAG, task
    with DAG(dag_id="bad-ecs", schedule="@daily"):
        with pytest.raises(ValueError, match="subnets"):
            @task.ecs_task(cluster="c", task_definition="td:1")  # FARGATE default, no subnets
            def e():
                pass


def test_ecs_invalid_launch_type_raises():
    from polyris import DAG, task
    with DAG(dag_id="bad-ecs-lt", schedule="@daily"):
        with pytest.raises(ValueError, match="launch_type"):
            @task.ecs_task(cluster="c", task_definition="td:1",
                           launch_type="fargate", subnets=["s-1"])
            def e():
                pass


def test_ecs_invalid_assign_public_ip_raises():
    from polyris import DAG, task
    with DAG(dag_id="bad-ecs-api", schedule="@daily"):
        with pytest.raises(ValueError, match="assign_public_ip"):
            @task.ecs_task(cluster="c", task_definition="td:1",
                           launch_type="FARGATE", subnets=["s-1"],
                           assign_public_ip="yes")
            def e():
                pass


def test_ecs_ec2_valid_launch_type():
    from polyris import DAG, task
    with DAG(dag_id="ecs-ec2-lt", schedule="@daily"):
        @task.ecs_task(cluster="c", task_definition="td:1", launch_type="EC2")
        def e():
            pass


def test_ecs_disabled_assign_public_ip_is_default():
    from polyris import DAG, task
    with DAG(dag_id="ecs-api-default", schedule="@daily"):
        @task.ecs_task(cluster="c", task_definition="td:1",
                       launch_type="FARGATE", subnets=["s-1"])
        def e():
            pass  # DISABLED is the default — must not raise


def test_glue_allocated_capacity_reaches_startjobrun(template):
    """allocated_capacity (the legacy DPU model, valid on its own) must reach
    startJobRun as AllocatedCapacity."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl", allocated_capacity=8)
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl"  # binding guard
    assert resolved.get("AllocatedCapacity") == 8, f"AllocatedCapacity dropped: {resolved!r}"
    assert "WorkerType" not in resolved


def test_glue_allocated_capacity_is_integer_in_task_config():
    """allocated_capacity must be stored as int in task_config — Glue StartJobRun
    rejects non-integer AllocatedCapacity. Tested at the task_config level so the
    assertion is not gated on the optional jsonata library."""
    from polyris import DAG, task
    from polyris.generators import _build_task_config_and_arn
    from polyris.constants import TaskConfigKey

    with DAG(dag_id="int-check-alloc", schedule="@daily") as dag:
        @task.glue_job(job_name="etl", allocated_capacity=4)
        def j():
            pass

    t = dag.tasks[0]
    task_config, _ = _build_task_config_and_arn(t)
    cap = task_config[TaskConfigKey.ALLOCATED_CAPACITY]
    assert cap == 4, f"wrong value: {cap!r}"
    assert isinstance(cap, int) and not isinstance(cap, bool), (
        f"AllocatedCapacity must be int in task_config, got {type(cap).__name__}: {cap!r}"
    )


def test_glue_number_of_workers_is_integer_in_task_config():
    """number_of_workers must be stored as int in task_config."""
    from polyris import DAG, task
    from polyris.generators import _build_task_config_and_arn
    from polyris.constants import TaskConfigKey

    with DAG(dag_id="int-check-workers", schedule="@daily") as dag:
        @task.glue_job(job_name="etl", worker_type="G.2X", number_of_workers=5)
        def j():
            pass

    t = dag.tasks[0]
    task_config, _ = _build_task_config_and_arn(t)
    n = task_config[TaskConfigKey.NUMBER_OF_WORKERS]
    assert n == 5, f"wrong value: {n!r}"
    assert isinstance(n, int) and not isinstance(n, bool), (
        f"NumberOfWorkers must be int in task_config, got {type(n).__name__}: {n!r}"
    )


def test_glue_python_shell_with_allocated_capacity(template):
    """command_name='pythonshell' + allocated_capacity is a valid Python Shell configuration."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl-py", command_name="pythonshell", allocated_capacity=1)
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl-py"
    assert resolved.get("AllocatedCapacity") == 1
    assert "WorkerType" not in resolved


def test_glue_pyspark_with_worker_type(template):
    """command_name='glueetl' + worker_type/number_of_workers is a valid PySpark configuration."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl-spark", command_name="glueetl",
                       worker_type="G.2X", number_of_workers=10)
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl-spark"
    assert resolved.get("WorkerType") == "G.2X"
    assert resolved.get("NumberOfWorkers") == 10
    assert "AllocatedCapacity" not in resolved


def test_glue_python_shell_rejects_worker_type():
    """command_name='pythonshell' is incompatible with worker_type/number_of_workers."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-py", schedule="@daily"):
        with pytest.raises(ValueError, match="pythonshell"):
            @task.glue_job(job_name="x", command_name="pythonshell",
                           worker_type="G.1X", number_of_workers=2)
            def j():
                pass


def test_glue_glueetl_rejects_allocated_capacity():
    """command_name='glueetl' is incompatible with allocated_capacity."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-spark", schedule="@daily"):
        with pytest.raises(ValueError, match="glueetl"):
            @task.glue_job(job_name="x", command_name="glueetl", allocated_capacity=8)
            def j():
                pass


def test_glue_invalid_command_name_raises():
    """An unknown command_name must raise immediately with an actionable message."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-cmd", schedule="@daily"):
        with pytest.raises(ValueError, match="command_name"):
            @task.glue_job(job_name="x", command_name="unknown")
            def j():
                pass


def test_glue_allocated_capacity_float_raises():
    """allocated_capacity=0.0625 must fail immediately with a message that
    points to max_capacity — the actual error the user saw was Glue rejecting
    AllocatedCapacity:0.0625 because it must be an INTEGER."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-float", schedule="@daily"):
        with pytest.raises(ValueError, match="max_capacity"):
            @task.glue_job(job_name="x", allocated_capacity=0.0625)
            def j():
                pass


def test_glue_max_capacity_in_task_config():
    """max_capacity must be stored as float in task_config and flow to MaxCapacity."""
    from polyris import DAG, task
    from polyris.generators import _build_task_config_and_arn
    from polyris.constants import TaskConfigKey

    with DAG(dag_id="max-cap-check", schedule="@daily") as dag:
        @task.glue_job(job_name="etl-py", max_capacity=0.0625)
        def j():
            pass

    t = dag.tasks[0]
    task_config, _ = _build_task_config_and_arn(t)
    cap = task_config[TaskConfigKey.MAX_CAPACITY]
    assert cap == 0.0625, f"wrong value: {cap!r}"
    assert isinstance(cap, float), f"MaxCapacity must be float, got {type(cap).__name__}: {cap!r}"
    assert TaskConfigKey.ALLOCATED_CAPACITY not in task_config


def test_glue_max_capacity_and_allocated_capacity_raises():
    """max_capacity and allocated_capacity are mutually exclusive."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-both-cap", schedule="@daily"):
        with pytest.raises(ValueError, match="mutually exclusive"):
            @task.glue_job(job_name="x", max_capacity=0.0625, allocated_capacity=1)
            def j():
                pass


def test_glue_max_capacity_and_worker_type_raises():
    """max_capacity is mutually exclusive with worker_type/number_of_workers."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-maxcap-worker", schedule="@daily"):
        with pytest.raises(ValueError, match="mutually exclusive"):
            @task.glue_job(job_name="x", max_capacity=0.0625,
                           worker_type="G.1X", number_of_workers=2)
            def j():
                pass


def test_glue_glueetl_rejects_max_capacity():
    """command_name='glueetl' is incompatible with max_capacity."""
    from polyris import DAG, task
    with DAG(dag_id="bad-glue-glueetl-maxcap", schedule="@daily"):
        with pytest.raises(ValueError, match="glueetl"):
            @task.glue_job(job_name="x", command_name="glueetl", max_capacity=1.0)
            def j():
                pass


def test_glue_max_capacity_reaches_startjobrun(template):
    """max_capacity must reach startJobRun as MaxCapacity (float) — not AllocatedCapacity."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl-py", max_capacity=0.0625)
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl-py"
    assert resolved.get("MaxCapacity") == 0.0625, f"MaxCapacity dropped: {resolved!r}"
    assert "AllocatedCapacity" not in resolved
    assert "WorkerType" not in resolved


def test_emr_rejects_non_dict_step():
    from polyris import DAG, task
    with DAG(dag_id="bad-emr3", schedule="@daily"):
        with pytest.raises(ValueError, match="must be a dict"):
            @task.emr_step(emr_cluster_id="j-ABC", emr_step="not-a-dict")
            def step():
                pass


# --- lambda: user payload merges UNDER orchestration context (G2, D1) ---

def test_lambda_user_payload_merges_under_orchestration(template):
    """A user-supplied payload flows into the Lambda Payload, but orchestration
    context (current_date / PARTITION_ARG / variables / upstream) overrides it on
    collision — so a stray 'current_date' in a user payload cannot corrupt
    backfill, and XCom-in (upstream) is never clobbered."""
    from polyris import task

    def build(dag):
        @task.lambda_function(
            function_name="arn:aws:lambda:us-east-1:111111111111:function:fn",
            payload={"my_key": "v", "current_date": "SHOULD_NOT_WIN"},
        )
        def fn():
            pass

    sdk_input = _wrapper_input_for(build)
    # run_task's input as the dispatcher builds it, and its state after Prepare_Task_Input
    runtime_input = {
        "task_arn": "arn:aws:lambda:us-east-1:111111111111:function:fn",
        "date": "2026-01-01",
        "partition_arg": "2026-01-01",
        "pipeline_name": "contract",
        "variables": {"myvar": "x"},
        "upstreams": {"up": {"status": "success", "skip_origin": None},
                      "audit": {"status": "failed", "skip_origin": None}},
        "upstream_outputs": {"up": {"k": "v"}},
        "task_config": sdk_input["task_config"],
    }
    resolved = _resolve_arguments(template, "Run_Task_Lambda", runtime_input)
    payload = resolved["Payload"]

    assert payload["current_date"] == "2026-01-01"  # binding guard + orchestration wins
    assert payload["PARTITION_ARG"] == "2026-01-01"
    assert payload.get("my_key") == "v"             # custom user key flows through
    assert payload["upstream"]["up"] == {"status": "success", "skip_origin": None, "output": {"k": "v"}}
    # an upstream that failed reaches the task as its status alone, never an older run's output (§1)
    assert payload["upstream"]["audit"] == {"status": "failed", "skip_origin": None}


# --- role: cross-account Credentials must apply to ALL wrapper-routed types (G6) ---

_WRAPPER_ROUTED_STATES = (
    "Run_Task_Lambda", "Run_Task_Glue", "Run_Task_ECS",
    "Run_Task_Athena", "Run_Task_EMR", "Run_Task_Batch",
)


def _resolve_role_arn(template, state_name, wrapper_input):
    jsonata = pytest.importorskip("jsonata")
    expr = template["States"][state_name]["Credentials"]["RoleArn"]
    j = jsonata.Jsonata(expr[2:-2].strip())
    j.assign("states", {"input": wrapper_input, "context": {"Execution": {"Input": wrapper_input}}})
    return j.evaluate({})


def test_role_credentials_apply_to_all_wrapper_types(template):
    """An explicit cross-account role ARN must reach Credentials.RoleArn for
    every wrapper-routed service type, not only sfn (G6). The Credentials block
    reads $states.input.cross_account_role, which the SDK threads for all types."""
    from polyris import task
    arn = "arn:aws:iam::999999999999:role/cross"

    def build(dag):
        @task.glue_job(job_name="etl", role=arn)
        def j():
            pass

    wi = _wrapper_input_for(build)
    assert wi["cross_account_role"] == arn  # SDK actually threaded it
    for state in _WRAPPER_ROUTED_STATES:
        assert _resolve_role_arn(template, state, wi) == arn, (
            f"{state} did not apply cross-account RoleArn"
        )


def test_role_same_falls_back_to_same_account(template):
    """role='same' must take the same-account fallback branch (the deploy-time
    ${same_account_role_arn}, normalized to '0' by the test fixture)."""
    from polyris import task

    def build(dag):
        @task.lambda_function(function_name="fn", role="same")
        def fn():
            pass

    wi = _wrapper_input_for(build)
    assert _resolve_role_arn(template, "Run_Task_Lambda", wi) == "0"


# --- retries: wrapper retry loop reads task_config (G7, ADR #107, retry loop, option B) ---

def _eval_jsonata(expr, wrapper_input, variables=None):
    jsonata = pytest.importorskip("jsonata")
    body = expr[2:-2].strip() if expr.startswith("{%") else expr
    j = jsonata.Jsonata(body)
    j.assign("states", {"input": wrapper_input, "context": {"Execution": {"Input": wrapper_input}}})
    for k, v in (variables or {}).items():
        j.assign(k, v)
    return j.evaluate({})


def test_retries_and_delay_reach_task_config():
    """task.retries / retry_delay must land in task_config so the wrapper loop
    can read them at runtime."""
    from datetime import timedelta
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl", retries=3, retry_delay=timedelta(seconds=45))
        def j():
            pass

    wi = _wrapper_input_for(build)
    assert wi["task_config"]["retries"] == 3
    assert wi["task_config"]["retry_delay"] == 45


def test_no_retries_leaves_task_config_untouched():
    """A task without retries must NOT add a retries key (the wrapper defaults to
    0), so no-retry tasks keep their existing task_config and incur no churn."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl")
        def j():
            pass

    wi = _wrapper_input_for(build)
    assert "retries" not in wi["task_config"]
    assert "retry_delay" not in wi["task_config"]


def test_retry_decision_condition(template):
    """Check_Should_Retry retries while attempt < task_config.retries, then stops."""
    cond = template["States"]["Check_Should_Retry"]["Choices"][0]["Condition"]
    ti = {"task_config": {"retries": 2}}
    assert _eval_jsonata(cond, ti, {"retry_attempt": 0}) is True
    assert _eval_jsonata(cond, ti, {"retry_attempt": 1}) is True
    assert _eval_jsonata(cond, ti, {"retry_attempt": 2}) is False  # exhausted
    # task with no retries configured -> never retries
    assert _eval_jsonata(cond, {"task_config": {"retries": 0}}, {"retry_attempt": 0}) is False


def test_retry_wait_reads_retry_delay(template):
    """Prepare_Retry_Wait computes retry_wait_seconds from task_config.retry_delay (dynamic).
    The formula was moved from Wait_Before_Retry.Seconds into an Assign so Save_Retry_Wait_Delay
    and Wait_Before_Retry both use the identical value (ADR #125)."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    assert _eval_jsonata(secs, {"task_config": {"retry_delay": 45}}) == 45
    # default when unset
    assert _eval_jsonata(secs, {"task_config": {}}) == 0


def test_retry_loop_closes_back_to_dispatch(template):
    """The retry path must form a terminating loop. Current shape (ADR #125):
    Check_Should_Retry → Prepare_Retry_Wait → Save_Retry_Wait_Delay → Wait_Before_Retry
    → Increment_Retry → Save_Retry_Attempt → Emit_Task_Retry_Started → Read_Upstream_Outputs: each attempt
    reads its upstreams' outputs again, since a failed read is a failed attempt too.
    """
    states = template["States"]
    assert states["Check_Should_Retry"]["Choices"][0]["Next"] == "Prepare_Retry_Wait"
    assert states["Prepare_Retry_Wait"]["Next"] == "Save_Retry_Wait_Delay"
    assert states["Save_Retry_Wait_Delay"]["Next"] == "Wait_Before_Retry"
    assert states["Wait_Before_Retry"]["Next"] == "Increment_Retry"
    inc = states["Increment_Retry"]
    assert inc["Next"] == "Save_Retry_Attempt"      # ADR #125 bookkeeping before dispatch
    assert "retry_attempt" in inc["Assign"]         # counter bump => loop terminates
    assert states["Save_Retry_Attempt"]["Next"] == "Emit_Task_Retry_Started"
    assert states["Emit_Task_Retry_Started"]["Next"] == "Read_Upstream_Outputs"  # re-dispatch


def test_decorators_reject_unknown_kwargs():
    """Variant decorators must fail fast on an unknown/typo'd parameter rather
    than silently swallowing it (D5 — **kwargs removed). Representative check."""
    from polyris import DAG, task
    with DAG(dag_id="strict", schedule="@daily"):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            @task.glue_job(job_name="x", job_nmae="typo")
            def j():
                pass


def test_every_task_decorator_accepts_common_kwargs():
    """Structural guard (ADR #109): every @task.<type> decorator MUST funnel
    shared parameters through **common, so a newly added task type can't silently
    drop a common capability (retries, timeouts, assets, ...). If someone adds a
    decorator without **common: Unpack[CommonTaskKwargs], this fails."""
    import inspect
    from polyris import task
    decorators = [
        n for n in dir(task)
        if not n.startswith("_") and callable(getattr(task, n))
    ]
    assert decorators, "no task decorators discovered"
    for name in decorators:
        sig = inspect.signature(getattr(task, name))
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        assert has_var_kw, (
            f"task.{name}() must accept **common (Unpack[CommonTaskKwargs]) so it "
            f"reuses shared task params — ADR #109."
        )


def test_all_task_types_wire_assets():
    """Behavioral counterpart: assets (outlets/inlets/wait_for) are common params,
    so EVERY task type must land them on the Task — not just sfn."""
    from polyris import DAG, task, Asset
    produced = Asset("prod", uri="s3://lake/prod/")
    consumed = Asset("cons", uri="s3://lake/cons/")
    specs = {
        "sfn": dict(arn="arn:aws:states:us-east-1:1:stateMachine:s"),
        "lambda_function": dict(function_name="fn"),
        "glue_job": dict(job_name="j"),
        "ecs_task": dict(cluster="c", task_definition="t:1", subnets=["subnet-x"]),
        "athena_query": dict(query_string="SELECT 1", database="db"),
        "emr_step": dict(
            emr_cluster_id="j-1",
            emr_step={"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["x"]}},
        ),
        "batch_job": dict(job_definition="d:1", job_queue="q"),
    }
    with DAG(dag_id="assets-all-types", schedule="@daily"):
        for name, kw in specs.items():
            @getattr(task, name)(outlets=[produced], inlets=[consumed], wait_for=[consumed], task_id=f"t_{name}", **kw)
            def _t():
                pass
            assert _t.outlets == [produced], f"task.{name} dropped outlets"
            assert _t.inlets == [consumed], f"task.{name} dropped inlets"
            assert _t.wait_for == [consumed], f"task.{name} dropped wait_for"


# --- athena: full param parity + workgroup-managed output nuance ---

def test_athena_params_reach_query_execution(template):
    from polyris import task

    def build(dag):
        @task.athena_query(query_string="SELECT 1", database="analytics",
                     output_location="s3://results/", workgroup="wg-x")
        def q():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Athena", wi)
    assert r["QueryString"] == "SELECT 1"
    assert r["QueryExecutionContext"]["Database"] == "analytics"
    assert r["WorkGroup"] == "wg-x"
    assert r["ResultConfiguration"]["OutputLocation"] == "s3://results/"


def test_athena_workgroup_defaults_to_primary(template):
    from polyris import task

    def build(dag):
        @task.athena_query(query_string="SELECT 1", database="db", output_location="s3://r/")
        def q():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Athena", wi)
    assert r["WorkGroup"] == "primary"


def test_athena_omits_result_config_when_output_unset(template):
    """No output_location => omit ResultConfiguration so a workgroup-enforced
    output location is used. Emitting OutputLocation:'' fails StartQueryExecution."""
    from polyris import task

    def build(dag):
        @task.athena_query(query_string="SELECT 1", database="db")  # workgroup-managed output
        def q():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Athena", wi)
    assert "ResultConfiguration" not in r, f"empty OutputLocation leaked: {r!r}"


# --- batch: full param parity ---

def test_batch_params_reach_submit_job(template):
    from polyris import task

    def build(dag):
        @task.batch_job(job_definition="jd:1", job_queue="jq", batch_parameters={"k": "v"})
        def b():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Batch", wi)
    assert r["JobDefinition"] == "jd:1"
    assert r["JobQueue"] == "jq"
    assert r["Parameters"] == {"k": "v"}


def test_batch_parameters_default_empty(template):
    from polyris import task

    def build(dag):
        @task.batch_job(job_definition="jd:1", job_queue="jq")
        def b():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Batch", wi)
    assert r["Parameters"] == {}


# --- ecs: tighten — Overrides / LaunchType / SecurityGroups also flow ---

def test_ecs_overrides_launchtype_securitygroups_reach_runtask(template):
    from polyris import task

    def build(dag):
        @task.ecs_task(cluster="c", task_definition="td:1", subnets=["s-1"],
                  launch_type="FARGATE", security_groups=["sg-1"],
                  container_overrides={"containerOverrides": [{"name": "app"}]})
        def e():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_ECS", wi)
    assert r["LaunchType"] == "FARGATE"
    assert r["Overrides"] == {"containerOverrides": [{"name": "app"}]}
    assert r["NetworkConfiguration"]["AwsvpcConfiguration"]["SecurityGroups"] == ["sg-1"]


# --- lambda: function_name reaches the invoke target ---

def test_lambda_function_name_reaches_invoke(template):
    from polyris import task

    def build(dag):
        @task.lambda_function(function_name="my-fn")
        def f():
            pass

    wi = _wrapper_input_for(build)
    # evaluate only FunctionName: the Payload $merge needs a runtime-resolved
    # `variables` object (Prepare_Task_Input builds it), covered separately.
    fn_expr = template["States"]["Run_Task_Lambda"]["Arguments"]["FunctionName"]
    assert _eval_jsonata(fn_expr, wi) == "my-fn"


# --- exponential backoff (ADR #107, retry loop, opt-in via retry_exponential_backoff) ---

def test_retry_wait_exponential_backoff_when_enabled(template):
    """With retry_backoff the wait doubles per attempt, capped at max_retry_delay."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True, "max_retry_delay": 60}}
    assert _eval_jsonata(secs, tc, {"retry_attempt": 0}) == 10   # 10 * 2^0
    assert _eval_jsonata(secs, tc, {"retry_attempt": 1}) == 20   # 10 * 2^1
    assert _eval_jsonata(secs, tc, {"retry_attempt": 2}) == 40   # 10 * 2^2
    assert _eval_jsonata(secs, tc, {"retry_attempt": 3}) == 60   # 80 -> capped at 60
    assert _eval_jsonata(secs, tc, {"retry_attempt": 9}) == 60   # stays capped


def test_retry_wait_backoff_uncapped_defaults_to_ceiling(template):
    """Backoff without max_retry_delay still bounds the wait at the 3600s default."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True}}  # no cap set
    assert _eval_jsonata(secs, tc, {"retry_attempt": 2}) == 40
    assert _eval_jsonata(secs, tc, {"retry_attempt": 20}) == 3600   # default ceiling


def test_retry_wait_fixed_when_backoff_disabled(template):
    """Without retry_backoff the wait stays fixed regardless of attempt (default)."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 10}}
    assert _eval_jsonata(secs, tc, {"retry_attempt": 0}) == 10
    assert _eval_jsonata(secs, tc, {"retry_attempt": 5}) == 10


def test_backoff_config_threaded_only_when_enabled():
    """retry_backoff + max_retry_delay reach task_config only when opted in."""
    from datetime import timedelta
    from polyris import task

    def build_on(dag):
        @task.glue_job(job_name="etl", retries=3, retry_delay=timedelta(seconds=10),
                   retry_exponential_backoff=True, max_retry_delay=timedelta(seconds=60))
        def j():
            pass

    tc = _wrapper_input_for(build_on)["task_config"]
    assert tc["retry_backoff"] is True
    assert tc["max_retry_delay"] == 60

    def build_off(dag):
        @task.glue_job(job_name="etl", retries=3, retry_delay=timedelta(seconds=10))
        def j():
            pass

    tc2 = _wrapper_input_for(build_off)["task_config"]
    assert "retry_backoff" not in tc2   # default: no backoff, no churn
    assert "max_retry_delay" not in tc2


def test_backoff_without_cap_omits_max_retry_delay():
    """Backoff enabled but no max_retry_delay: retry_backoff is set, max_retry_delay
    is omitted (wrapper falls back to its default ceiling)."""
    from datetime import timedelta
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl", retries=2, retry_delay=timedelta(seconds=5),
                   retry_exponential_backoff=True)  # no max_retry_delay
        def j():
            pass

    tc = _wrapper_input_for(build)["task_config"]
    assert tc["retry_backoff"] is True
    assert "max_retry_delay" not in tc


# --- retry jitter (ADR #107, retry loop, opt-in equal jitter via $random) ---

def test_retry_wait_jitter_bounds_over_backoff(template):
    """With retry_jitter over exponential backoff, each wait is a random integer in
    [base/2, base) where base = min(retry_delay*2^attempt, cap)."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True,
                          "max_retry_delay": 60, "retry_jitter": True}}
    vals = [_eval_jsonata(secs, tc, {"retry_attempt": 2}) for _ in range(200)]  # base=40
    assert all(float(v).is_integer() for v in vals)
    assert all(20 <= v < 40 for v in vals), (min(vals), max(vals))
    assert len(set(vals)) > 1  # genuinely randomised, not a constant


def test_retry_wait_jitter_bounds_over_fixed(template):
    """Jitter also applies over a fixed (non-backoff) delay: [base/2, base)."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 45, "retry_jitter": True}}
    vals = [_eval_jsonata(secs, tc, {"retry_attempt": 3}) for _ in range(200)]
    assert all(22 <= v < 45 for v in vals), (min(vals), max(vals))


def test_retry_wait_no_jitter_is_deterministic(template):
    """Without retry_jitter the wait is exactly the computed value (no randomness)."""
    secs = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True, "max_retry_delay": 60}}
    vals = {_eval_jsonata(secs, tc, {"retry_attempt": 2}) for _ in range(20)}
    assert vals == {40}


def test_jitter_config_threaded_only_when_enabled():
    """retry_jitter reaches task_config only when opted in (works with or without backoff)."""
    from datetime import timedelta
    from polyris import task

    def build_on(dag):
        @task.glue_job(job_name="etl", retries=2, retry_delay=timedelta(seconds=10),
                   retry_jitter=True)
        def j():
            pass

    assert _wrapper_input_for(build_on)["task_config"]["retry_jitter"] is True

    def build_off(dag):
        @task.glue_job(job_name="etl", retries=2, retry_delay=timedelta(seconds=10))
        def j():
            pass

    assert "retry_jitter" not in _wrapper_input_for(build_off)["task_config"]


# --- primitive value parsing (S3 fix) and multi-upstream merge (S4) ---

def _step_functions_parse(model):
    """$parse as Step Functions may have it (sfn_jsonata.parse_json): JSON null stays null. A text that is no JSON
    fails the state ("throws", the model every other test uses) or gives undefined; which one AWS does is
    checked live in TR-7, and run_task must hold for both."""
    from sfn_jsonata import parse_json

    def _parse(text):
        try:
            return parse_json(text)
        except ValueError:
            if model == "throws":
                raise
            return None

    return _parse


PARSE_MODELS = ("throws", "gives_undefined")


def _eval_get_dep_output(template, ddb_raw_value, dep_name="dep_task", *, cap=25000, status="success",
                         parse="throws"):
    """Evaluate Get_Dep_Output.Output with a simulated DDB getItem result (None: no output# item).

    The inner Map processor's input is one item of Read_Upstream_Outputs' Items: the upstream's name, its
    status and the size up to which its output is inlined. Registers $parse as Step Functions has it.
    """
    jsonata = pytest.importorskip("jsonata")
    inner_states = template["States"]["Read_Upstream_Outputs"]["ItemProcessor"]["States"]
    expr = inner_states["Get_Dep_Output"]["Output"]
    j = jsonata.Jsonata(expr[2:-2].strip())
    j.register_lambda("parse", _step_functions_parse(parse))
    item = {"result": {"S": ddb_raw_value}} if ddb_raw_value is not None else None
    j.assign("states", {"result": {"Item": item} if item else {},
                        "input": {"name": dep_name, "status": status, "cap": cap}})
    return j.evaluate({})


@pytest.mark.parametrize("parse", PARSE_MODELS)
@pytest.mark.parametrize("raw, expected", [
    ('{"k": 1}', {"k": 1}), ('[1, 2, 3]', [1, 2, 3]), ('null', None), ('true', True), ('42', 42),
    ('"ok"', "ok"),                    # a string result, as 1.3 stores it
    ('ok', "ok"), ('', ""),            # ... and as a 1.2 run_task stored it: no JSON, so never parsed
])
def test_an_upstream_output_reaches_the_task_as_the_value_its_task_returned(template, parse, raw, expected):
    """The review of TR-5: a string result stored as it was failed $parse, and its dependant never ran."""
    result = _eval_get_dep_output(template, raw, parse=parse)
    assert result["found"] is True and result["output"] == expected


def test_a_text_that_starts_like_json_and_is_not_reaches_the_task_without_an_output(template):
    """A 1.2 row's string result such as "[INFO] done": $parse fails the state or gives undefined. Either way the
    item is found without an output, and xcom reads the table, which returns the string."""
    from sfn_jsonata import evaluate
    assert "output" not in _eval_get_dep_output(template, "[INFO] done", parse="gives_undefined")
    with pytest.raises(Exception, match="Expecting"):
        _eval_get_dep_output(template, "[INFO] done", parse="throws")
    states = template["States"]["Read_Upstream_Outputs"]["ItemProcessor"]["States"]
    [catch] = [c for c in states["Get_Dep_Output"]["Catch"] if c["ErrorEquals"] == ["States.QueryEvaluationError"]]
    assert states[catch["Next"]] == {"Type": "Pass", "End": True}
    assert evaluate(catch["Output"], input={"name": "extract", "status": "success", "cap": 100}) == {
        "name": "extract", "status": "success", "found": True}


@pytest.mark.parametrize("state, field", [("Run_Task_SFN", "Output"), ("Run_Task_Lambda", "Payload")])
@pytest.mark.parametrize("value, stored, read", [
    ("ok", '"ok"', "ok"), ("", '""', ""), ('say "hi"', '"say \\"hi\\""', 'say "hi"'),     # text: a JSON string
    ('{"a": 1}', '{"a": 1}', {"a": 1}), ("[1]", "[1]", [1]), ("123", "123", 123),         # JSON text: as it is,
    ("true", "true", True),                                                               # parsed, as in 1.2
    ({"rows": 1}, '{"rows":1}', {"rows": 1}), ([1, "a"], '[1,"a"]', [1, "a"]), (42, "42", 42), (True, "true", True),
    (None, "null", None)])
def test_a_result_is_stored_as_json_text_as_1_2_read_it(template, state, field, value, stored, read):
    """A string that starts like JSON is JSON text (json.dumps), stored as it is and read parsed, as in 1.2; any
    other string is stored as a JSON string, since "ok" stored as it was is no JSON, and no reader could parse it
    (the reviews of TR-5 and TR-5e). The reader decides "starts like JSON" by the same characters."""
    from jsonata.utils import Utils
    from sfn_jsonata import evaluate
    result = {field: Utils.NULL_VALUE if value is None else value}
    out = evaluate(template["States"][state]["Output"], input={"upstream_outputs": {}}, context=_context({}),
                   result=result)
    assert out["task_output"] == stored and json.loads(out["task_output"]) == read
    jsonish = re.search(r"in (\[[^\]]*\]) \?", template["States"][state]["Output"]).group(1)
    assert jsonish in template["States"]["Read_Upstream_Outputs"]["ItemProcessor"]["States"]["Get_Dep_Output"][
        "Output"]
    assert evaluate(template["States"][state]["Output"], input={}, context=_context({}), result={})[
        "task_output"] == "{}"


def test_an_output_over_its_share_of_the_budget_reaches_the_task_as_a_marker(template):
    """xcom reads a marked output from the table; below 64 characters a share holds no marker either."""
    big = '{"rows": "' + "x" * 300 + '"}'
    assert _eval_get_dep_output(template, big, cap=100)["output"] == {"_truncated": True, "_size": len(big)}
    assert "output" not in _eval_get_dep_output(template, big, cap=63)
    assert _eval_get_dep_output(template, None) == {"name": "dep_task", "status": "success", "found": False}


def test_an_output_over_256_kb_reaches_the_task_as_a_marker(template):
    """xcom.push may store up to 350 KB, which the read cannot carry: the item counts as found (D109), and the
    marker sends xcom, of 1.3 or of an older bundle, to the table."""
    get = template["States"]["Read_Upstream_Outputs"]["ItemProcessor"]["States"]["Get_Dep_Output"]
    catch = [c for c in get["Catch"] if c["ErrorEquals"] == ["States.DataLimitExceeded"]]
    assert catch and catch[0]["Next"] == "Dep_Output_Too_Large"
    from sfn_jsonata import evaluate
    assert evaluate(catch[0]["Output"], input={"name": "extract", "status": "success", "cap": 1}) == {
        "name": "extract", "status": "success", "found": True, "output": {"_truncated": True}}


def _items(template, run_input):
    from sfn_jsonata import evaluate
    return evaluate(template["States"]["Read_Upstream_Outputs"]["Items"], input={},
                    context={"Execution": {"Input": run_input}})


def test_only_an_upstream_that_succeeded_or_was_skipped_by_an_operator_or_the_run_is_read(template):
    """ADR #137 §1: any other upstream reaches the task as its status alone, so a task that runs after a
    failure never reads an older run's output of the same date."""
    from polyris.constants import SkipOrigin
    upstreams = {"a": {"status": "success", "skip_origin": None},
                 "b": {"status": "skipped", "skip_origin": SkipOrigin.MANUAL},
                 "c": {"status": "skipped", "skip_origin": SkipOrigin.RUN},
                 "d": {"status": "skipped", "skip_origin": SkipOrigin.RULE},
                 "e": {"status": "failed", "skip_origin": None},
                 "f": {"status": "upstream_failed", "skip_origin": None},
                 "g": {"status": "stopped", "skip_origin": None}}
    items = _items(template, {"upstreams": upstreams, "pipeline_name": "p", "date": "2026-11-18"})
    assert sorted((i["name"], i["status"]) for i in items) == [("a", "success"), ("b", "skipped"),
                                                                ("c", "skipped")]
    assert _items(template, {"upstreams": {}}) == []


def test_the_set_run_task_reads_is_the_set_xcom_reads(template):
    """Principle #28: an upstream whose output run_task does not read must be one xcom refuses to read from the
    table, or a task would get an older run's output of the date (the review of TR-5)."""
    from polyris.constants import SkipOrigin, TaskStatus
    from polyris.xcom import _readable
    for status in [s.value for s in TaskStatus] + ["unknown", None]:
        for origin in [SkipOrigin.MANUAL, SkipOrigin.RUN, SkipOrigin.RULE, None]:
            upstreams = {"u": {"status": status, "skip_origin": origin}}
            read = bool(_items(template, {"upstreams": upstreams}))
            assert read == _readable("u", status, origin, raise_on_failure=False, raise_on_missing=False), (
                status, origin)


def test_the_outputs_inlined_share_a_budget_with_the_input(template):
    """The state and the work's arguments carry the input and the inlined outputs, under 256 KB together."""
    upstreams = {f"up{i}": {"status": "success", "skip_origin": None} for i in range(4)}
    small = _items(template, {"upstreams": upstreams})
    assert {i["cap"] for i in small} == {25000}                              # 1.2's cap per output
    padded = {"upstreams": upstreams, "variables": {"blob": "x" * 150000}}
    cap = _items(template, padded)[0]["cap"]
    assert cap == (200000 - len(json.dumps(padded, separators=(",", ":")))) // 4
    assert _items(template, {"upstreams": upstreams, "variables": {"blob": "x" * 250000}})[0]["cap"] == 0


def _after_map(template, results, state_input=None):
    jsonata = pytest.importorskip("jsonata")
    j = jsonata.Jsonata(template["States"]["Read_Upstream_Outputs"]["Output"][2:-2].strip())
    j.assign("states", {"result": results, "input": state_input or {}})
    return j.evaluate({})


def test_two_producers_one_consumer(template):
    """Both upstream outputs reach the downstream Lambda event['upstream'].

    Exercises the Read_Upstream_Outputs $merge($map(...)) expression with 2
    deps — previously untested with more than 1 upstream. Regression gate for
    the multi-upstream merge silently dropping one entry.

    Pattern from scenario-4/dag.py:
        [extract_core_sales(), enrich_customer_segments()] >> daily_report()
    """
    dep1 = _eval_get_dep_output(template, '{"sales_count": 100}', dep_name="extract_core_sales")
    dep2 = _eval_get_dep_output(template, '{"segments": ["vip", "new"]}', dep_name="enrich_customer_segments")
    after_map = _after_map(template, [dep1, dep2])
    assert after_map == {"upstream_outputs": {"extract_core_sales": {"sales_count": 100},
                                              "enrich_customer_segments": {"segments": ["vip", "new"]}},
                         "missing": []}

    runtime_input = {
        **after_map,
        "task_arn": "arn:aws:lambda:us-east-1:111111111111:function:polyris-test-lambda",
        "task_config": {"payload": {}}, "variables": {}, "partition_arg": "2026-01-01", "date": "2026-01-01",
        "pipeline_name": "scenario-4",
        "upstreams": {"extract_core_sales": {"status": "success", "skip_origin": None},
                      "enrich_customer_segments": {"status": "success", "skip_origin": None}},
    }
    payload = _resolve_arguments(template, "Run_Task_Lambda", runtime_input)["Payload"]
    assert payload["upstream"]["extract_core_sales"]["output"] == {"sales_count": 100}
    assert payload["upstream"]["enrich_customer_segments"]["output"] == {"segments": ["vip", "new"]}


def test_an_upstream_that_succeeded_without_its_output_fails_the_task_without_retries(template):
    """D109: its success wrote the output in the same transaction, so a missing one is not transient."""
    states = template["States"]
    found = _eval_get_dep_output(template, '{"k": 1}', dep_name="extract")
    lost = _eval_get_dep_output(template, None, dep_name="load")
    skipped = _eval_get_dep_output(template, None, dep_name="audit", status="skipped")
    after = _after_map(template, [found, lost, skipped], {"variables": {}})
    assert after["missing"] == ["load"]                       # a skipped upstream may have none
    assert states["Check_Upstream_Outputs"]["Choices"][0]["Next"] == "Missing_Upstream_Output"
    from sfn_jsonata import evaluate
    failed = evaluate(states["Missing_Upstream_Output"]["Output"], input=after)
    assert failed["error_obj"]["Error"] == "UpstreamOutputMissing" and "load" in failed["error"]
    assert states["Missing_Upstream_Output"]["Next"] == "Get_Decision_Timeout"


def test_a_failed_read_of_the_outputs_is_a_failed_attempt(template):
    catches = _catches(template["States"]["Read_Upstream_Outputs"])
    assert catches["States.ALL"] == catches["States.DataLimitExceeded"] == "Shape_Failure"


def test_save_task_input_state_removed(template):
    """The old monolithic Save_Task_Input is gone, and so is Init_Output_Row: the success transaction
    clears a stale push marker itself."""
    assert "Save_Task_Input" not in template["States"]
    assert "Init_Output_Row" not in template["States"]
    assert "Save_Input_Record" in template["States"]


def test_save_input_record_uses_input_key_prefix(template):
    """Task_input now lives in a separate DDB record with `input#...` key —
    lets Console preview hold up to ~380KB instead of the old 25KB shared cap."""
    key = template["States"]["Save_Input_Record"]["Arguments"]["Key"]["execution_name"]["S"]
    assert "'input#'" in key
    assert "'output#'" not in key


def test_save_input_record_no_truncation_expression(template):
    """The 25KB $length check is gone — the new record has its
    own 400KB DDB item budget."""
    ti_expr = template["States"]["Save_Input_Record"]["Arguments"]["ExpressionAttributeValues"][":ti"]["S"]
    assert "25000" not in ti_expr, "task_input must not truncate at 25KB anymore"
    assert "_upstream_omitted" not in ti_expr, "task_input must not emit _upstream_omitted marker anymore"


def test_save_input_record_uses_updateitem_not_putitem(template):
    """Concurrent-safe semantics: same-date backfill runs share the input# key;
    putItem would clobber a prior run's task_input mid-flight."""
    assert template["States"]["Save_Input_Record"]["Resource"] == "arn:aws:states:::dynamodb:updateItem"


def test_save_input_record_uses_task_date_not_date(template):
    """Field name deliberately mismatched from date-pipeline-index
    GSI's key attribute to avoid populating that GSI with internal input# rows.
    is_internal_record() also filters input#* by prefix as belt-and-suspenders."""
    update_expr = template["States"]["Save_Input_Record"]["Arguments"]["UpdateExpression"]
    assert "task_date = :td" in update_expr
    # And must NOT write bare 'date' — that would pollute the GSI
    for clause in update_expr.split(","):
        clause = clause.strip()
        assert not clause.startswith("date "), (
            f"Save_Input_Record must not write bare 'date' field (GSI pollution risk): {clause!r}"
        )


def test_the_input_record_lives_90_days_from_each_write(template):
    """D82/D108: input# keeps 90 days, counted from its latest write (retention.INPUT)."""
    arguments = template["States"]["Save_Input_Record"]["Arguments"]
    assert "#ttl = :ttl" in arguments["UpdateExpression"] and "if_not_exists" not in arguments["UpdateExpression"]
    assert "input_ttl_seconds" in arguments["ExpressionAttributeValues"][":ttl"]["N"]


def test_prepare_task_input_chains_to_save_input_record(template):
    """Wiring: Prepare_Task_Input -> Save_Input_Record -> Check_Task_Type."""
    assert template["States"]["Prepare_Task_Input"]["Next"] == "Save_Input_Record"
    assert template["States"]["Save_Input_Record"]["Next"] == "Check_Task_Type"


def test_save_input_record_catch_falls_through_to_check_task_type(template):
    """Best-effort: DDB failure on the input write must not block dispatch."""
    catches = _catches(template["States"]["Save_Input_Record"])
    assert catches["States.ALL"] == catches["States.DataLimitExceeded"] == "Check_Task_Type"


# ── Push detection: Check_Task_Pushed + Save_Success_Preserve ────────


def _eval_with_context(template, state_path, wrapper_input,
                       context_execution_id="arn:aws:states:us-east-1:111111111111:execution:wrapper:current-run",
                       result_dict=None):
    """Evaluate a state's Output (or nested field) expression with $states.context bound.

    jsonata-python doesn't auto-bind $states.context — so tests that read
    $states.context.Execution.Id must supply it explicitly. state_path is a
    dotted path from template['States'] to the JSONata expression string.
    """
    jsonata = pytest.importorskip("jsonata")
    node = template["States"]
    for part in state_path.split("."):
        node = node[part]
    expr = node
    body = expr[2:-2].strip() if isinstance(expr, str) and expr.startswith("{%") else expr
    j = jsonata.Jsonata(body)
    states = {
        "input": wrapper_input,
        "context": {"Execution": {"Id": context_execution_id}},
    }
    if result_dict is not None:
        states["result"] = result_dict
    j.assign("states", states)
    return j.evaluate({})


def test_check_task_pushed_state_exists(template):
    assert "Check_Task_Pushed" in template["States"]
    state = template["States"]["Check_Task_Pushed"]
    assert state["Type"] == "Task"
    assert state["Resource"] == "arn:aws:states:::dynamodb:getItem"


def test_check_task_pushed_projects_marker_and_run_id(template):
    """GetItem must read _pushed_by_task AND pushed_run_id for versioning."""
    state = template["States"]["Check_Task_Pushed"]
    proj = state["Arguments"]["ProjectionExpression"]
    assert "#p" in proj
    assert "pushed_run_id" in proj
    assert state["Arguments"]["ExpressionAttributeNames"]["#p"] == "_pushed_by_task"


def test_check_task_pushed_reads_output_key_prefix(template):
    """Marker lives on the canonical output# row (same row xcom.push() writes)."""
    key = template["States"]["Check_Task_Pushed"]["Arguments"]["Key"]["execution_name"]["S"]
    assert "'output#'" in key


def test_check_task_pushed_matches_current_run_id(template):
    """B1: matching pushed_run_id → _task_pushed = true. Verifies happy path."""
    result_matching = {"Item": {
        "_pushed_by_task": {"BOOL": True},
        "pushed_run_id": {"S": "arn:aws:states:us-east-1:111111111111:execution:wrapper:current-run"},
    }}
    output = _eval_with_context(
        template,
        "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        context_execution_id="arn:aws:states:us-east-1:111111111111:execution:wrapper:current-run",
        result_dict=result_matching,
    )
    assert output["_task_pushed"] is True


def test_check_task_pushed_rejects_stale_run_id(template):
    """B1 KEY GUARANTEE: marker with wrong run_id → _task_pushed = false.
    Without this rejection, a backfill re-run would inherit the previous
    run's pushed value and silently corrupt downstream reads."""
    result_stale = {"Item": {
        "_pushed_by_task": {"BOOL": True},
        "pushed_run_id": {"S": "arn:aws:states:us-east-1:111111111111:execution:wrapper:OLD-run"},
    }}
    output = _eval_with_context(
        template,
        "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        context_execution_id="arn:aws:states:us-east-1:111111111111:execution:wrapper:current-run",
        result_dict=result_stale,
    )
    assert output["_task_pushed"] is False


def test_check_task_pushed_no_marker_no_push(template):
    """No marker → _task_pushed = false (normal path for Lambda/SFN tasks)."""
    result_empty = {"Item": {}}
    output = _eval_with_context(
        template,
        "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        result_dict=result_empty,
    )
    assert output["_task_pushed"] is False


def test_check_task_pushed_marker_false_no_push(template):
    """Marker explicitly false (theoretically possible) → _task_pushed = false."""
    result_false = {"Item": {"_pushed_by_task": {"BOOL": False}}}
    output = _eval_with_context(
        template,
        "Check_Task_Pushed.Output",
        wrapper_input={"pipeline_name": "p", "task_name": "t", "date": "d"},
        result_dict=result_false,
    )
    assert output["_task_pushed"] is False


def test_a_push_check_that_fails_counts_as_a_success_not_recorded(template):
    """Writing the work's return over an output the task pushed would lose it: the operator decides."""
    assert _catches(template["States"]["Check_Task_Pushed"])["States.ALL"] == "Success_Not_Recorded"
    assert template["States"]["Check_Task_Pushed"]["Next"] == "Record_Success"


# ── The success transaction (ADR #137 §1, invariant 12) ──────────────

SUCCESS_RUN = {"execution_name": "load-2026-11-18-orders-run-x", "generation": 3, "task_name": "load",
               "pipeline_name": "orders", "pipeline_execution": "orders-run-2026-11-18-x", "date": "2026-11-18",
               "cascade": "auto", "outlets": [{"name": "ns/orders", "uri": "s3://bucket/orders"},
                                              {"name": "ns/daily", "granularity": "daily"}],
               "output_ttl_seconds": 31536000, "history_ttl_seconds": 39312000,
               "partition_date": "2026-11-18", "granularity": "daily"}     # run_task_input.partition_fields


def _transaction(template, *, pushed=False, run=None, output='{"rows": 3}'):
    from sfn_jsonata import evaluate
    state = {"task_output": output, "task_execution_arn": "jr_1", "_task_pushed": pushed}
    return evaluate(template["States"]["Record_Success"]["Arguments"], input=state,
                    context=_context(run or SUCCESS_RUN))["TransactItems"]


def test_the_success_is_one_transaction_of_the_status_the_output_and_the_facts(template):
    from sfn_jsonata import NOW, plain
    from polyris.coordination import retention, task_row
    from polyris.coordination.times import format_utc
    row, output, *facts = _transaction(template)
    update = row["Update"]
    assert update["Key"] == {"execution_name": {"S": SUCCESS_RUN["execution_name"]}}
    assert update["ConditionExpression"] == f"#s = :running AND {task_row.FENCE}"
    values = update["ExpressionAttributeValues"]
    assert (values[":running"], values[":success"], values[":g"]) == ({"S": "running"}, {"S": "success"}, {"N": "3"})
    assert "REMOVE #att, #due" in update["UpdateExpression"]                    # no attention entry on success
    assert values[":ttl"] == {"N": str(retention.ttl(NOW, retention.HISTORY))}
    out = output["Update"]
    assert out["Key"] == {"execution_name": {"S": "output#orders#load#2026-11-18"}}
    assert out["ExpressionAttributeValues"][":r"] == {"S": '{"rows": 3}'}
    assert out["ExpressionAttributeValues"][":ttl"] == {"N": str(retention.ttl(NOW, retention.OUTPUT))}
    assert [plain(f["Put"]["Item"])["asset_name"] for f in facts] == ["ns/orders", "ns/daily"]
    assert {f["Put"]["ConditionExpression"] for f in facts} == {"attribute_not_exists(event_time)"}
    assert {plain(f["Put"]["Item"])["event_time"] for f in facts} == {format_utc(NOW)}


def test_a_fact_in_the_transaction_has_the_shape_of_asset_fact_item(template):
    from sfn_jsonata import NOW, plain
    from polyris.coordination.records import asset_fact_item
    _, _, fact, daily = _transaction(template)
    expected = asset_fact_item(asset_name="ns/orders", partition_key="2026-11-18", granularity="daily",
                               cascade="auto", source_dag="orders", source_task="load", uri="s3://bucket/orders",
                               now=NOW, ttl_days=365)
    written = plain(fact["Put"]["Item"])
    assert {k: v for k, v in written.items() if k != "ttl"} == {k: v for k, v in expected.items() if k != "ttl"}
    assert plain(daily["Put"]["Item"])["uri"] == ""


def test_a_task_without_outlets_records_its_status_and_output_alone(template):
    assert len(_transaction(template, run={**SUCCESS_RUN, "outlets": []})) == 2


def test_an_output_the_task_pushed_survives_its_success(template):
    """xcom.push by this execution wrote the result: the success writes the status around it. Otherwise the
    work's return replaces the result, and a stale push marker of an earlier run goes."""
    pushed = _transaction(template, pushed=True)[1]["Update"]
    assert "#r" not in pushed["UpdateExpression"] and ":r" not in pushed["ExpressionAttributeValues"]
    assert "REMOVE" not in pushed["UpdateExpression"]
    returned = _transaction(template)[1]["Update"]
    assert "#r = :r" in returned["UpdateExpression"]
    assert "REMOVE #p, pushed_at, pushed_run_id, push_count" in returned["UpdateExpression"]
    assert returned["ExpressionAttributeNames"]["#p"] == "_pushed_by_task"


def _after_cancel(template, item):
    jsonata = pytest.importorskip("jsonata")
    j = jsonata.Jsonata(template["States"]["Read_After_Cancel"]["Output"][2:-2].strip())
    j.assign("states", {"input": {}, "result": {"Item": item} if item else {},
                        "context": _context(SUCCESS_RUN)})
    return j.evaluate(None)


def _route(template, state, record_tries):
    from sfn_jsonata import evaluate
    choice = template["States"]["Route_After_Cancel"]
    for rule in choice["Choices"]:
        if evaluate(rule["Condition"], input=state, variables={"record_tries": record_tries}):
            return rule["Next"]
    return choice["Default"]


def test_a_cancelled_transaction_reads_the_row_and_branches_on_it(template):
    """ADR #137 §1: its own success under its generation is a lost answer; a raised generation ends it quietly;
    any other cause tries again, with a new time for each fact, and then waits for a decision."""
    states = template["States"]
    assert _catches(states["Record_Success"])["DynamoDb.TransactionCanceledException"] == "Read_After_Cancel"
    succeeded = _after_cancel(template, {"status": {"S": "success"}, "generation": {"N": "3"}})
    running = _after_cancel(template, {"status": {"S": "running"}, "generation": {"N": "3"}})
    raised = _after_cancel(template, {"status": {"S": "stopped"}, "generation": {"N": "4"}})
    assert _route(template, succeeded, 1) == "Emit_Task_Finished_Success"
    assert _route(template, running, 1) == "Wait_Before_Recording_Again"
    assert _route(template, running, 5) == "Success_Not_Recorded"
    assert _route(template, raised, 1) == "Superseded"
    assert _route(template, _after_cancel(template, None), 1) == "Superseded"   # the row is gone
    assert states["Wait_Before_Recording_Again"]["Next"] == "Record_Success"


def test_a_success_that_cannot_be_recorded_waits_for_a_decision_with_its_cause(template):
    """The transaction's Catch names States.DataLimitExceeded too (TR-0 B2). Any other error may have applied
    the write, so the row is read first: a lost answer is a success, a miss is sent again up to five times
    (the review of TR-5). The work runs again only if an operator restarts it."""
    from sfn_jsonata import evaluate
    states = template["States"]
    catches = _catches(states["Record_Success"])
    assert catches["States.DataLimitExceeded"] == "Success_Not_Recorded"
    assert catches["States.ALL"] == catches["DynamoDb.TransactionCanceledException"] == "Read_After_Cancel"
    route = states["Route_After_Cancel"]
    assert route["Choices"][-1]["Next"] == "Success_Not_Recorded" and "$record_tries < 5" in route["Choices"][1][
        "Condition"]
    waiting = evaluate(states["Success_Not_Recorded"]["Output"],
                       input={"record_error": {"Error": "States.DataLimitExceeded", "Cause": "too large"},
                              "task_output": "x"})
    assert waiting["error_obj"]["Error"] == "SuccessNotRecorded" and "too large" in waiting["error"]
    assert "task_output" not in waiting
    assert states["Success_Not_Recorded"]["Next"] == "Get_Decision_Timeout"


#: The writes the row does not depend on: a permanent error there (a missing grant, an input# over 400 KB) costs
#: seconds before the work starts or ends (the review of TR-5e).
BEST_EFFORT = {"Emit_Task_Started", "Emit_Task_Retry_Started", "Emit_Task_Finished_Success",
               "Emit_Task_Finished_Failed", "Save_Input_Record"}


def test_every_dynamodb_state_retries_any_error_but_an_answer_for_minutes(template):
    """The review of TR-5: three tries in seven seconds, on error names nobody had seen, failed a claim for good and
    left a success waiting for a decision. A condition that failed is an answer, and an expression error or an
    oversized payload does not pass on a retry; anything else is retried for about two minutes, whatever name AWS
    gives it (the optimized integration says DynamoDB.*, the SDK one DynamoDb.*, TR-0 B3)."""
    never = {"DynamoDB.ConditionalCheckFailedException", "DynamoDb.ConditionalCheckFailedException",
             "DynamoDb.TransactionCanceledException", "States.QueryEvaluationError", "States.DataLimitExceeded"}
    found = []

    def walk(states):
        for name, state in states.items():
            if ":dynamodb:" in str(state.get("Resource", "")):
                found.append(name)
                first, rest = state["Retry"]
                assert set(first["ErrorEquals"]) == never and first["MaxAttempts"] == 0, name
                assert rest["ErrorEquals"] == ["States.ALL"] and rest["MaxDelaySeconds"] == 60, name
                waited = sum(min(rest["IntervalSeconds"] * rest["BackoffRate"] ** i, 60)
                             for i in range(rest["MaxAttempts"]))
                if name in BEST_EFFORT:
                    assert waited <= 10, name      # a timeline event or the console's preview: seconds, not minutes
                else:
                    assert waited >= (14 if name == "Record_Success" else 120), name   # the success: five entries
            if "ItemProcessor" in state:
                walk(state["ItemProcessor"]["States"])

    walk(template["States"])
    assert {"Claim", "Read_Claim", "Ask", "Record_Success", "Read_After_Cancel", "Get_Dep_Output"} <= set(found)


def test_a_lambda_that_crashed_or_timed_out_is_a_failed_attempt(template):
    """Lambda.Unknown (a crash, a timeout, out of memory) ran the user's code again outside the task's retries, as
    the States.Timeout retry did (the review of TR-5); the service's own errors are retried, the code did not run."""
    errors = {e for r in template["States"]["Run_Task_Lambda"]["Retry"] for e in r["ErrorEquals"]}
    assert "Lambda.Unknown" not in errors and {"Lambda.ServiceException", "Lambda.TooManyRequestsException"} <= errors


def test_a_long_task_id_keeps_the_childs_name_unique(template):
    """A task id of up to 128 bytes: the name ends in a hash of this run_task execution and of this entry of the
    work state, never the clock, so another run, another generation or a retry never meets ExecutionAlreadyExists,
    while the state's own retry of the start sends the same name (the reviews of TR-5 and TR-5e: six digits of
    the milliseconds repeat every 1,000 seconds)."""
    from sfn_jsonata import evaluate
    expr = template["States"]["Run_Task_SFN"]["Arguments"]["Name"]
    key = "t" * 128 + "-2026-11-18--2026-11-18-0000abcd"

    def name(run_id, entered, attempt=1, row=key):
        context = {"Execution": {"Input": {"execution_name": row}, "Id": run_id, "Name": "x"},
                   "State": {"EnteredTime": entered}}
        return evaluate(expr, context=context, variables={"attempt": attempt})

    first = name("arn:run-1", "2026-11-18T06:00:00.000Z")
    assert first == name("arn:run-1", "2026-11-18T06:00:00.000Z")                  # its own retry: the same
    others = {name("arn:run-2", "2026-11-18T06:00:00.000Z"), name("arn:run-1", "2026-11-18T06:05:00.000Z")}
    assert first not in others and len(others) == 2
    assert all(len(n) <= 80 and re.search(r"-[0-9a-f]{12}$", n) for n in others | {first})
    short = name("arn:run-1", "2026-11-18T06:00:00.000Z", attempt=2, row="extract-2026-11-18--2026-11-18-0000abcd")
    assert short.startswith("restart-extract-2026-11-18--2026-11-18-0000abcd-")


def test_a_batch_job_is_named_after_its_run_task_execution(template):
    """Batch takes at most 128 letters, digits, - and _: a long task id or a dot in it failed every SubmitJob."""
    from polyris import limits
    from polyris.coordination import run_name
    assert template["States"]["Run_Task_Batch"]["Arguments"]["JobName"] == "{% $states.context.Execution.Name %}"
    assert len(limits.batch_submit_job({}, "p", "t", {})["JobName"]) == run_name.MAX_NAME <= 128


@pytest.mark.parametrize("cause, arn", [
    ('{"ExecutionArn":"arn:aws:states:us-east-1:1:execution:child:abc","Input":"{\\"rows\\": \\"xxx',
     "arn:aws:states:us-east-1:1:execution:child:abc"),               # cut at 32,768 characters: no JSON
    ('{"ExecutionArn": "arn:x"}', "arn:x"), ("the user's task failed", ""), ("", "")])
def test_a_failures_cause_is_matched_for_its_arn_never_parsed(template, cause, arn):
    """A cause Step Functions cut is no JSON, and $parse failed the catcher itself: no retry, no decision, and the
    wrong cause 10 to 15 minutes later (the review of TR-5). Every catcher matches the ARN instead."""
    from sfn_jsonata import evaluate
    states = template["States"]
    catcher = next(c for c in states["Run_Task_SFN"]["Catch"] if c["ErrorEquals"] == ["States.ALL"])
    caught = evaluate(catcher["Output"], input={"upstream_outputs": {}, "x": 1}, context=_context({}),
                      error_output={"Error": "States.TaskFailed", "Cause": cause})
    assert caught == {"x": 1, "caught": {"Error": "States.TaskFailed", "Cause": cause}}
    out = evaluate(states["Shape_Failure"]["Output"], input=caught, context=_context({}))
    assert out["task_execution_arn"] == arn and out["error_obj"]["Cause"] == cause and "caught" not in out
    assert "$parse($causeStr)" not in json.dumps(template)


def test_every_failure_of_the_work_is_shaped_in_one_place(template):
    """Sixteen copies of one expression became one state; each catcher only keeps what it caught."""
    states = template["States"]
    workers = ["Read_Upstream_Outputs"] + [f"Run_Task_{t}" for t in ("SFN", "Lambda", "Glue", "ECS", "Athena",
                                                                      "EMR", "Batch")]
    for name in workers:
        for catcher in states[name]["Catch"]:
            assert catcher["Next"] == "Shape_Failure", name
            assert "caught" in catcher["Output"], name
    assert states["Shape_Failure"]["Next"] == "Check_Should_Retry"


def test_a_task_that_waits_for_a_decision_ends_its_timeline_with_the_failure(template):
    """1.2 wrote TASK_FINISHED failed with error_summary, which the console's timeline shows; run_task writes it
    once the row waits for the decision, with the fields records.failure_event gives the dispatcher's failures."""
    from polyris.coordination import records
    from sfn_jsonata import NOW, evaluate
    states = template["States"]
    assert states["Ask"]["Next"] == "Emit_Task_Finished_Failed"
    emit = states["Emit_Task_Finished_Failed"]
    assert emit["Next"] == "Check_Is_Backfill" and _catches(emit)["States.ALL"] == "Check_Is_Backfill"
    run_input = {"execution_name": "load-x", "pipeline_execution": "orders-run-x", "date": "2026-11-21",
                 "task_name": "load", "timeline_ttl_seconds": 7776000}
    item = evaluate(emit["Arguments"]["Item"], input={"error": "e" * 600}, context=_context(run_input),
                    variables={"attempt": 1, "retry_attempt": 2})
    assert item["retry_attempt"] == {"N": "2"}             # "Task failed after retry 2", as 1.2 showed
    model = records.failure_event(task_run_id="x", execution_name="x", task_name="x", pipeline_execution="x",
                                  partition_key="x", backfill_id="", now=NOW, cause="e" * 600)
    assert set(model) <= set(item) and item["status"] == {"S": "failed"}
    assert item["error_summary"] == {"S": model["error_summary"]}
    assert item["event_time"]["S"].endswith(records.FINISHED_EVENT_ORDER)


def test_the_row_keeps_at_most_4000_characters_of_the_error(template):
    """Every full pass reads the row, and attention-index projects it (the review of TR-5)."""
    from sfn_jsonata import evaluate
    value = template["States"]["Ask"]["Arguments"]["ExpressionAttributeValues"][":error"]["S"]
    kept = evaluate(value, input={"error": "e" * 50000}, context=_context({}))
    assert kept == "e" * 4000 + "...[truncated]"
    assert evaluate(value, input={"error": "short"}, context=_context({})) == "short"


@pytest.mark.parametrize("date, current_date, date_iso, previous_date, month", [
    ("2026-11-18", "2026-11-18", "2026-11-18", "2026-11-17", "11"),
    ("2026-11-18T06", "2026-11-18T06", "2026-11-18", "2026-11-17", "11"),
    ("2026-W47", "2026-W47", "2026-11-16", "2026-11-15", "11"),
    ("2026-W01", "2026-W01", "2025-12-29", "2025-12-28", "12"),
    ("2026-11", "2026-11", "2026-11-01", "2026-10-31", "11"),
])
def test_the_date_variables_of_any_partition_are_those_of_its_first_day(template, date, current_date, date_iso,
                                                                         previous_date, month):
    """current_date and partition_key stay the run's key; every day form is the partition's first day's, which the
    input carries (run_task_input.partition_fields). $toMillis of a weekly or hourly key failed every task."""
    from polyris.coordination import run_task_input
    from sfn_jsonata import evaluate
    run_input = {"date": date, "partition_arg": date, "variables": {}, **run_task_input.partition_fields(date)}
    variables = evaluate(template["States"]["Prepare_Task_Input"]["Output"], input={},
                         context=_context(run_input))["variables"]
    assert (variables["current_date"], variables["partition_key"]) == (current_date, current_date)
    assert (variables["date_iso"], variables["previous_date"], variables["month"]) == (date_iso, previous_date, month)
    assert variables["date_compact"] == date_iso.replace("-", "")


@pytest.mark.parametrize("backfill_id, expected", [("bf-123", True), ("", False), (None, False)])
def test_is_backfill_is_true_in_a_run_of_a_backfill(template, backfill_id, expected):
    """A backfill's runs carry its backfill_id (Start_Run, the dispatcher); is_backfill was always false."""
    from sfn_jsonata import evaluate
    run_input = {"date": "2026-11-18", "partition_arg": "2026-11-18", "partition_date": "2026-11-18",
                 "variables": {}}
    if backfill_id is not None:
        run_input["backfill_id"] = backfill_id
    out = evaluate(template["States"]["Prepare_Task_Input"]["Output"], input={}, context=_context(run_input))
    assert out["variables"]["is_backfill"] is expected
    assert out["variables"]["backfill_id"] == (backfill_id or "")


def test_all_run_task_states_route_to_check_task_pushed(template):
    """Every Run_Task_* state must feed into Check_Task_Pushed, not Save_Success directly.
    Otherwise pushed values would be silently overwritten on the default flow."""
    for state_name in ["Run_Task_SFN", "Run_Task_Lambda", "Run_Task_Glue",
                       "Run_Task_ECS", "Run_Task_Athena", "Run_Task_EMR", "Run_Task_Batch"]:
        state = template["States"][state_name]
        assert state["Next"] == "Check_Task_Pushed", (
            f"{state_name}.Next must be Check_Task_Pushed (was: {state['Next']}) — "
            "otherwise xcom.push() marker check is bypassed on this task type."
        )


# ── POLYRIS_TASK_NAME + POLYRIS_WRAPPER_RUN_ID env injection ───


def test_glue_arguments_include_task_name_and_run_id_env(template):
    """xcom.push() from a Glue job reads POLYRIS_TASK_NAME (DDB key) and
    POLYRIS_WRAPPER_RUN_ID (stale-marker rejection). Must reach Glue as
    --POLYRIS_* arguments so the job's arg parser can expose them as env-like
    values via getResolvedOptions()."""
    from polyris import task

    def build(dag):
        @task.glue_job(job_name="etl")
        def j():
            pass

    wi = _wrapper_input_for(build)
    # Pass context_execution_id so $states.context.Execution.Id resolves — otherwise
    # JSONata drops undefined values from dict-emitting expressions and the KEY vanishes.
    run_id = "arn:aws:states:us-east-1:1:execution:wrapper:test-run"
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi, context_execution_id=run_id)
    args = resolved["Arguments"]
    assert args["--POLYRIS_TASK_NAME"] == "j"
    assert args["--POLYRIS_WRAPPER_RUN_ID"] == run_id
    # Existing env keys still present — regression guard.
    assert args["--POLYRIS_PIPELINE_NAME"] == "contract"
    assert "--POLYRIS_RUN_DATE" in args
    assert args["--POLYRIS_TOKENS_TABLE"] == "0"  # ${tokens_table} normalized


def test_ecs_container_env_includes_task_name_and_run_id(template):
    """ECS containers receive POLYRIS_* as Environment entries so xcom.push()
    can read them via os.environ.

    NOTE: env injection only fires when the user passes container_overrides
    with the AWS-correct PascalCase 'ContainerOverrides' key. Lowercase
    'containerOverrides' is a pre-existing pass-through path (documented by
    test_ecs_overrides_launchtype_securitygroups_reach_runtask) — SFN template
    reads $ov.ContainerOverrides literally.
    """
    from polyris import task

    def build(dag):
        @task.ecs_task(cluster="c", task_definition="td:1", subnets=["s-1"],
                       container_overrides={"ContainerOverrides": [{"Name": "app"}]})
        def e():
            pass

    wi = _wrapper_input_for(build)
    run_id = "arn:aws:states:us-east-1:1:execution:wrapper:test-run"
    resolved = _resolve_arguments(template, "Run_Task_ECS", wi, context_execution_id=run_id)
    env = resolved["Overrides"]["ContainerOverrides"][0]["Environment"]
    names = {e["Name"] for e in env}
    assert "POLYRIS_TASK_NAME" in names
    assert "POLYRIS_WRAPPER_RUN_ID" in names
    # Regression: existing env vars still injected
    assert "POLYRIS_PIPELINE_NAME" in names
    assert "POLYRIS_RUN_DATE" in names
    assert "POLYRIS_TOKENS_TABLE" in names
    # Values are correct
    by_name = {e["Name"]: e["Value"] for e in env}
    assert by_name["POLYRIS_TASK_NAME"] == "e"
    assert by_name["POLYRIS_WRAPPER_RUN_ID"] == run_id


def test_batch_container_env_includes_task_name_and_run_id(template):
    """Batch job container env — same as ECS."""
    from polyris import task

    def build(dag):
        @task.batch_job(job_definition="jd:1", job_queue="jq")
        def b():
            pass

    wi = _wrapper_input_for(build)
    run_id = "arn:aws:states:us-east-1:1:execution:wrapper:test-run"
    resolved = _resolve_arguments(template, "Run_Task_Batch", wi, context_execution_id=run_id)
    env = resolved["ContainerOverrides"]["Environment"]
    names = {e["Name"] for e in env}
    assert "POLYRIS_TASK_NAME" in names
    assert "POLYRIS_WRAPPER_RUN_ID" in names
    # Regression
    assert "POLYRIS_PIPELINE_NAME" in names
    assert "POLYRIS_RUN_DATE" in names
    assert "POLYRIS_TOKENS_TABLE" in names
    by_name = {e["Name"]: e["Value"] for e in env}
    assert by_name["POLYRIS_TASK_NAME"] == "b"
    assert by_name["POLYRIS_WRAPPER_RUN_ID"] == run_id


def test_emr_does_not_inject_task_name_env(template):
    """EMR xcom.push() is DEFERRED — no Environment field in
    addStep.sync (only HadoopJarStep.Args, which would break user's Spark arg
    parsers if we injected there unconditionally). Documented as future work.
    This test pins that decision — if it fails, we shipped EMR push support and
    need to also document the arg-passing convention in DATA_PASSING.md."""
    from polyris import task
    jar = "s3://bucket/spark.jar"

    def build(dag):
        @task.emr_step(
            emr_cluster_id="j-ABC",
            emr_step={
                "Name": "Spark",
                "ActionOnFailure": "CONTINUE",
                "HadoopJarStep": {"Jar": jar, "Args": ["--date", "2026-01-01"]},
            },
        )
        def step():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_EMR", wi)
    # Args should be exactly what the user passed — no POLYRIS_* injected.
    args = resolved["Step"]["HadoopJarStep"]["Args"]
    assert "--POLYRIS_TASK_NAME" not in args
    assert "--POLYRIS_WRAPPER_RUN_ID" not in args


# =============================================================================
# ADR #125 — In-SFN retry visibility (retry_attempt field + TASK_RETRY_STARTED)
# =============================================================================

class TestRetryVisibility:
    """Structural guards for the ADR #125 contract.

    Changes mean little without protection against regression; these tests pin
    the invariants a future refactor is most likely to break silently."""

    @pytest.fixture(autouse=True)
    def _tpl(self, template):
        self.rt = template

    def _task_events_emitters(self):
        """Every state that writes to ${task_events_table} via dynamodb:putItem."""
        for name, body in self.rt["States"].items():
            if body.get("Resource") != "arn:aws:states:::dynamodb:putItem":
                continue
            args = body.get("Arguments", {})
            if args.get("TableName") == "${task_events_table}":
                yield name, body

    def test_every_task_event_emitter_carries_attempt_and_retry_attempt(self):
        """Regression gate for the original Emit_Task_Started bug: it emitted no
        `attempt` field, so the backend defaulted every start to attempt=1 and
        the UI could not render a bumped attempt. Every emitter of a task-events
        row must carry both axes (ADR #125)."""
        missing_attempt: list[str] = []
        missing_retry_attempt: list[str] = []
        for name, body in self._task_events_emitters():
            item = body.get("Arguments", {}).get("Item", {})
            if "attempt" not in item:
                missing_attempt.append(name)
            if "retry_attempt" not in item:
                missing_retry_attempt.append(name)
        assert not missing_attempt, (
            f"task-events emitters missing `attempt` field: {missing_attempt}. "
            "Add `\"attempt\": {\"N\": ...}` to each Item (ADR #125 regression gate)."
        )
        assert not missing_retry_attempt, (
            f"task-events emitters missing `retry_attempt` field: {missing_retry_attempt}. "
            "Add `\"retry_attempt\": {\"N\": ...}` to each Item (ADR #125 regression gate)."
        )

    def test_save_retry_attempt_is_fenced_by_the_generation(self):
        """A run_task whose row an operator took over (a raised generation) writes nothing more and ends
        quietly (ADR #137 invariant 5, Rule 47)."""
        from polyris.coordination import task_row
        s = self.rt["States"]["Save_Retry_Attempt"]
        assert s["Arguments"]["ConditionExpression"] == f"#s = :running AND {task_row.FENCE}"
        catches = s["Catch"]
        guard_catch = next(c for c in catches if "DynamoDB.ConditionalCheckFailedException" in c["ErrorEquals"])
        assert guard_catch["Next"] == "Superseded"

    def test_save_retry_attempt_throttle_fallthrough_routes_to_emit(self):
        """On DDB throttle, Save_Retry_Attempt must fall through to
        Emit_Task_Retry_Started so events remain authoritative even when the row
        lags (ADR #125 § Consequences — "events are source of truth")."""
        s = self.rt["States"]["Save_Retry_Attempt"]
        catches = s["Catch"]
        states_all_catch = next(c for c in catches if "States.ALL" in c["ErrorEquals"])
        assert states_all_catch["Next"] == "Emit_Task_Retry_Started", (
            "States.ALL catch on Save_Retry_Attempt must route to Emit_Task_Retry_Started, "
            "not Stale_Attempt_Superseded — DDB throttle must not silently kill the retry."
        )

    def test_emit_task_retry_started_event_time_is_unique_per_attempt(self):
        """With retry_delay=0 the same wall-clock millisecond can hold multiple
        emits. Encoding $retry_attempt into the sort key makes the composite
        (task_run_id, event_time) PK unique so DynamoDB does not silently
        overwrite earlier retry rows."""
        emit = self.rt["States"]["Emit_Task_Retry_Started"]
        event_time_expr = emit["Arguments"]["Item"]["event_time"]["S"]
        assert "$retry_attempt" in event_time_expr, (
            "event_time must include $retry_attempt — otherwise two retries in "
            "the same ms produce the same PK and overwrite in task-events."
        )
        # And the suffix must sort AFTER TASK_STARTED's #10 so the timeline is
        # chronological; our convention is `#15-N`.
        assert "'#15-'" in event_time_expr

    def test_emit_task_retry_started_event_type_literal(self):
        """ADR #125 — the new event type literal is pinned by the parity test
        (test_task_event_type_parity.py); this test additionally pins it is the
        literal emitted here, not a typo like TASK_RETRY_START."""
        emit = self.rt["States"]["Emit_Task_Retry_Started"]
        assert emit["Arguments"]["Item"]["event_type"]["S"] == "TASK_RETRY_STARTED"

    def test_the_claim_resets_retry_attempt(self):
        """Each start (the first, or after a Restart) begins with retry_attempt=0 so the UI badge and the
        row agree; a Restart would otherwise inherit the previous start's count (ADR #125)."""
        claim = self.rt["States"]["Claim"]
        assert "retry_attempt = :zero" in claim["Arguments"]["UpdateExpression"]
        assert claim["Arguments"]["ExpressionAttributeValues"][":zero"] == {"N": "0"}

    def test_save_retry_wait_delay_writes_countdown_fields(self):
        """The retry Wait must surface via the same wait_delay_until_ms /
        wait_delay_started_ms fields the UI's CountdownTimer already consumes
        for pause/delay waits — one display path, not two."""
        s = self.rt["States"]["Save_Retry_Wait_Delay"]
        upd = s["Arguments"]["UpdateExpression"]
        assert "wait_delay_started_ms = :started_ms" in upd
        assert "wait_delay_until_ms = :until_ms" in upd
        # And the until_ms derivation must use the retry_wait_seconds variable
        # that Prepare_Retry_Wait assigned — otherwise the row and the Wait
        # could drift (one jitter sample vs another).
        until_ms_expr = s["Arguments"]["ExpressionAttributeValues"][":until_ms"]["N"]
        assert "$retry_wait_seconds" in until_ms_expr

    def test_wait_before_retry_uses_prepared_variable(self):
        """Wait_Before_Retry.Seconds must read the $retry_wait_seconds variable
        assigned in Prepare_Retry_Wait — reusing the same value as Save_Retry_Wait_Delay
        so the UI countdown and the actual sleep agree to the millisecond."""
        wait = self.rt["States"]["Wait_Before_Retry"]
        assert wait["Seconds"] == "{% $retry_wait_seconds %}"

    def test_increment_retry_output_strips_prior_error(self):
        """ADR #107 — the retry loop must drop the prior attempt's error from
        the forwarded input so the re-dispatch does not accidentally see a
        stale `error` field. Pinned to prevent regression after the loop
        rewiring in ADR #125."""
        inc = self.rt["States"]["Increment_Retry"]
        output = inc["Output"]
        assert "'error'" in output
        assert "'error_obj'" in output
        assert "'task_execution_arn'" in output


# =============================================================================
# ADR #137 §1 — run_task only runs the work
# =============================================================================

WORK_STATES = ("Run_Task_SFN", "Run_Task_Lambda", "Run_Task_Glue", "Run_Task_ECS", "Run_Task_Athena",
               "Run_Task_EMR", "Run_Task_Batch")


def test_run_task_reads_only_the_keys_of_its_input_and_every_one_of_them():
    """ADR #137 §9: the dispatcher builds the input from run_task_input.Key; the template reads it from its
    execution's context, so a key on one side only is a contract drift."""
    from polyris.coordination import run_task_input
    read = set(re.findall(r"\$states\.context\.Execution\.Input\.([a-zA-Z_]+)", TEMPLATE_PATH.read_text()))
    assert read == run_task_input.KEYS


def test_each_attempt_has_the_tasks_time_limit_and_a_timeout_is_not_retried_by_the_state(template):
    """execution_timeout bounds one attempt (§1); a timeout goes through the task's retries, so the work
    states no longer retry States.Timeout themselves, which ran one attempt up to three times."""
    from sfn_jsonata import evaluate
    for name in WORK_STATES:
        state = template["States"][name]
        assert evaluate(state["TimeoutSeconds"], context=_context({"execution_timeout": 900})) == 900
        assert evaluate(state["TimeoutSeconds"], context=_context({})) == 99999999       # no limit of its own
        assert "States.Timeout" not in {e for r in state.get("Retry", []) for e in r["ErrorEquals"]}, name
        assert _catches(state)["States.ALL"] == _catches(state)["States.DataLimitExceeded"] == "Shape_Failure"


@pytest.mark.parametrize("error,delay,expected", [("States.Timeout", 5, 60), ("States.Timeout", 90, 90),
                                                  ("States.TaskFailed", 5, 5)])
def test_a_retry_after_a_timeout_waits_a_minute_at_least(template, error, delay, expected):
    """Step Functions stops a timed-out .sync job within 15 to 60 seconds (TR-0 B5): two copies of the work
    must not overlap."""
    from sfn_jsonata import evaluate
    assign = template["States"]["Prepare_Retry_Wait"]["Assign"]["retry_wait_seconds"]
    run = {"task_config": {"retries": 2, "retry_delay": delay}}
    assert evaluate(assign, input={"error_obj": {"Error": error}}, context=_context(run),
                    variables={"retry_attempt": 0}) == expected


def test_a_task_that_fails_after_its_retries_waits_for_a_decision_and_run_task_ends(template):
    """running -> waiting_decision with the cause, the time the wait began and the timeout in force; its
    attention entry falls due at the deadline (ADR #137 §1, §6). No process waits for the decision."""
    from datetime import timedelta
    from sfn_jsonata import NOW, evaluate
    from polyris.coordination import attention, retention, task_row
    from polyris.coordination.times import format_utc
    states = template["States"]
    assert states["Check_Should_Retry"]["Default"] == "Get_Decision_Timeout"
    assert states["Get_Decision_Timeout"]["Next"] == "Ask"
    arguments = states["Ask"]["Arguments"]
    assert arguments["ConditionExpression"] == f"#s = :running AND {task_row.FENCE}"
    run = {"execution_name": "load-2026-11-18-orders-run-x", "generation": 3, "history_ttl_seconds": 39312000}
    values = evaluate(arguments["ExpressionAttributeValues"], input={"error": "boom"}, context=_context(run),
                      variables={"decision_timeout_seconds": 600})
    assert values[":waiting_decision"] == {"S": "waiting_decision"} and values[":error"] == {"S": "boom"}
    assert values[":entry"] == {"S": attention.attention(attention.DECISION, run["execution_name"])}
    assert values[":deadline"] == {"S": format_utc(NOW + timedelta(seconds=600))}
    assert values[":now"] == {"S": format_utc(NOW)} and values[":timeout"] == {"N": "600"}
    assert values[":ttl"] == {"N": str(retention.ttl(NOW, retention.HISTORY))}
    for name in ("#att", "#due"):
        assert arguments["ExpressionAttributeNames"][name] in (attention.KEY, attention.DUE)
    assert _catches(states["Ask"]) == {"DynamoDB.ConditionalCheckFailedException": "Superseded",
                                       "States.ALL": "Ask_Failed"}
    assert states["Ask_Failed"]["Type"] == "Fail"
    assert states["Send_PagerDuty_Alert"]["Next"] == states["Check_Is_Backfill"]["Choices"][0]["Next"] \
        == "Waiting_For_Decision"
    assert states["Waiting_For_Decision"]["Type"] == "Succeed"


def test_the_decision_timeout_falls_back_to_the_consoles_default(template):
    """ADR #114: __global_settings__ holds the timeout; both sides read 5 hours when it holds none."""
    repo = (TEMPLATE_PATH.parents[3] / "lambdas" / "console_api" / "dal" / "pipelines_repo.py").read_text()
    default = int(re.search(r"DEFAULT_DECISION_TIMEOUT_SECONDS = (\d+)", repo).group(1))
    state = template["States"]["Get_Decision_Timeout"]
    jsonata = pytest.importorskip("jsonata")
    expression = jsonata.Jsonata(state["Assign"]["decision_timeout_seconds"][2:-2].strip())
    expression.assign("states", {"result": {}})
    assert expression.evaluate(None) == default == state["Catch"][0]["Assign"]["decision_timeout_seconds"]


@pytest.mark.parametrize("state,kind", [("Run_Task_Glue", "glue"), ("Run_Task_ECS", "ecs"),
                                        ("Run_Task_Batch", "batch")])
def test_a_glue_ecs_or_batch_task_gets_its_upstreams_statuses_for_xcom(template, state, kind):
    """POLYRIS_UPSTREAM_STATUSES carries what run_task_input.upstream_statuses_env builds (§9, Principle #28)."""
    from polyris import task
    from polyris.coordination import run_task_input
    from polyris.xcom import ENV_UPSTREAM_STATUSES
    upstreams = {"extract": ("failed", None), "audit": ("skipped", "manual")}

    def build(dag):
        if kind == "glue":
            @task.glue_job(job_name="etl")
            def load():
                pass
        elif kind == "ecs":
            @task.ecs_task(cluster="c", task_definition="td", subnets=["s-1"],
                           container_overrides={"ContainerOverrides": [{"Name": "app"}]})
            def load():
                pass
        else:
            @task.batch_job(job_definition="jd", job_queue="jq")
            def load():
                pass

    resolved = _resolve_arguments(template, state, _wrapper_input_for(build, upstreams=upstreams),
                                  context_execution_id=RUN_ARN)
    if kind == "glue":
        value = resolved["Arguments"][f"--{ENV_UPSTREAM_STATUSES}"]
    else:
        env = (resolved["Overrides"]["ContainerOverrides"][0] if kind == "ecs" else resolved["ContainerOverrides"])
        value = {e["Name"]: e["Value"] for e in env["Environment"]}[ENV_UPSTREAM_STATUSES]
    assert value == run_task_input.upstream_statuses_env(upstreams)


def test_the_notify_calls_pass_their_fields_explicitly(template):
    """1.2's Resolve_PagerDuty read fields its input never carried, so it never ran: each call names them."""
    from sfn_jsonata import evaluate
    run = {"pipeline_name": "orders", "task_name": "load", "execution_name": "load-2026-11-18-x",
           "date": "2026-11-18"}
    for name in ("Interactive_Slack", "Send_PagerDuty_Alert", "Resolve_PagerDuty"):
        payload = evaluate(template["States"][name]["Arguments"]["Payload"], input={}, context=_context(run))
        assert payload["pipeline_name"] == "orders", name
        assert {k: payload["failure"][k] for k in run} == run, name
    resolve = template["States"]["Should_Resolve"]
    assert resolve["Choices"][0]["Next"] == "Resolve_PagerDuty" and resolve["Default"] == "Done"
    assert evaluate(resolve["Choices"][0]["Condition"], variables={"attempt": 1}) is False
    assert evaluate(resolve["Choices"][0]["Condition"], variables={"attempt": 2}) is True


def test_every_write_of_the_row_renews_its_history_and_every_event_keeps_90_days(template):
    """D108: a row lives a year and 90 days from its latest write; the timeline 90 days (retention)."""
    for name, state in template["States"].items():
        arguments = state.get("Arguments")
        if not isinstance(arguments, dict):
            continue
        if arguments.get("TableName") == "0" and arguments.get("Key", {}).get("execution_name", {}).get(
                "S", "").endswith("Input.execution_name %}") and "UpdateExpression" in arguments:
            assert "#ttl = :ttl" in arguments["UpdateExpression"], name
            assert "history_ttl_seconds" in arguments["ExpressionAttributeValues"][":ttl"]["N"], name
        if state.get("Resource") == "arn:aws:states:::dynamodb:putItem":
            item = arguments["Item"]
            assert "timeline_ttl_seconds" in item["ttl"]["N"], name
            assert item["task_run_id"] == {"S": "{% $states.context.Execution.Name %}"}, name


def test_the_deploy_counts_the_environment_run_task_adds_to_an_ecs_task(template):
    """D118: polyris-deploy measures an ECS task's overrides with what Run_Task_ECS adds (limits.PLATFORM_ENV)."""
    from polyris import limits, task

    def build(dag):
        @task.ecs_task(cluster="c", task_definition="td", subnets=["s-1"],
                       container_overrides={"ContainerOverrides": [{"Name": "app"}]})
        def load():
            pass

    resolved = _resolve_arguments(template, "Run_Task_ECS", _wrapper_input_for(build),
                                  context_execution_id=RUN_ARN)
    env = resolved["Overrides"]["ContainerOverrides"][0]["Environment"]
    assert [e["Name"] for e in env] == list(limits.PLATFORM_ENV)
    by_name = {e["Name"]: e["Value"] for e in env}
    assert len(by_name["POLYRIS_RUN_DATE"]) <= limits.PLATFORM_ENV["POLYRIS_RUN_DATE"]
    assert len(RUN_ARN) <= limits.PLATFORM_ENV["POLYRIS_WRAPPER_RUN_ID"]


def test_the_deploy_counts_what_run_task_sends_a_batch_task(template):
    """D118: polyris-deploy measures a Batch task's SubmitJob request as Run_Task_Batch builds it
    (limits.batch_submit_job), against Batch's 30 KiB."""
    from polyris import limits
    sent = template["States"]["Run_Task_Batch"]["Arguments"]
    env = re.findall(r"'Name': '(POLYRIS_\w+)'", sent["ContainerOverrides"])
    assert env == list(limits.PLATFORM_ENV)
    modelled = limits.batch_submit_job({}, "p", "t", {})
    assert set(modelled) == set(sent) and set(modelled["ContainerOverrides"]) == {"Environment"}


def test_the_date_variables_fit_the_bound_the_deploy_counts(template):
    """D118: a Lambda's or a state machine's payload carries the date variables beside what the input holds."""
    from polyris import limits
    from polyris.coordination import run_task_input
    from sfn_jsonata import evaluate
    date = limits._LONGEST_DATE
    run_input = {"date": date, "partition_arg": date, "backfill_id": "x" * 64, "variables": {},
                 **run_task_input.partition_fields(date)}
    variables = evaluate(template["States"]["Prepare_Task_Input"]["Output"], input={},
                         context=_context(run_input))["variables"]
    assert len(json.dumps(variables, separators=(",", ":"), ensure_ascii=False).encode()) <= limits.DATE_VARIABLES_MAX
