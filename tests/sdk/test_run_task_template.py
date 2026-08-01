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


def test_start_state_is_skip_check(template):
    """The very first state must be the skip_tasks check — otherwise
    skipped tasks would still write status=running, fetch dependencies,
    etc., before being noped out. Regression for ADR #51 task_subset."""
    assert template["StartAt"] == "Check_Should_Skip_Task", (
        f"StartAt must be Check_Should_Skip_Task; got: {template['StartAt']}"
    )


def test_skip_tasks_choice_exists(template):
    """run_task must have a Choice state at the start that compares
    task_name against the skip_tasks list."""
    states = template["States"]
    assert "Check_Should_Skip_Task" in states, (
        "Check_Should_Skip_Task missing — task_subset backfill ignored."
    )
    choice = states["Check_Should_Skip_Task"]
    assert choice["Type"] == "Choice"
    cond = choice["Choices"][0]["Condition"]
    assert "skip_tasks" in cond, f"Condition must reference skip_tasks: {cond}"
    assert "task_name" in cond, f"Condition must reference task_name: {cond}"
    # Must guard against missing field (no NPE for scheduled runs)
    assert "$exists" in cond, (
        f"Condition must use $exists to guard missing skip_tasks: {cond}"
    )


def test_task_skipped_terminal_state(template):
    """Skipped task must reach a terminal state (End: true) emitting
    a synthetic 'skipped' status. Downstream tasks with trigger_rule
    'all_done' rely on this."""
    states = template["States"]
    assert "Task_Skipped" in states
    skipped = states["Task_Skipped"]
    assert skipped["Type"] == "Pass"
    assert skipped.get("End") is True
    output_expr = skipped["Output"]
    assert "'skipped'" in output_expr, (
        f"Skipped output must include status='skipped': {output_expr}"
    )
    assert "task_name" in output_expr


def test_skip_check_default_preserves_existing_flow(template):
    """Critical for backward compat: when skip_tasks empty/missing,
    Default branch must continue to the existing entry point. Otherwise
    every scheduled pipeline run would break."""
    choice = template["States"]["Check_Should_Skip_Task"]
    assert choice["Default"] == "Check_Execution_Paused", (
        f"Default branch must preserve existing flow; got: {choice['Default']}"
    )


def test_emit_asset_events_honors_suppress_flag(template):
    """Check_Has_Outlets must guard Emit_Asset_Events with
    _suppress_asset_event. Regression for cascade='none' backfill
    (ADR #57) — without this, isolated backfills would still emit
    asset events and trigger downstream consumers."""
    states = template["States"]
    choice = states["Check_Has_Outlets"]
    cond = choice["Choices"][0]["Condition"]
    assert "_suppress_asset_event" in cond, (
        f"Check_Has_Outlets condition must check _suppress_asset_event for cascade='none' backfill semantics: {cond}"
    )
    # Default must still skip to next state without emitting (existing flow)
    assert choice["Default"] == "Check_Orchestration_Token_Success"


def test_pause_check_still_runs_for_normal_tasks(template):
    """After the skip check, normal tasks must still hit Check_Execution_Paused
    so pause/resume semantics are preserved."""
    states = template["States"]
    assert "Check_Execution_Paused" in states
    # Route_Pause_Check still leads to Update_Status_Running (existing flow)
    assert states["Route_Pause_Check"]["Default"] == "Update_Status_Running"


def test_update_status_running_has_attempt_guard(template):
    """Update_Status_Running must carry a ConditionExpression that blocks a
    ghost (prior-attempt) execution from overwriting run_task_helper_arn or
    resetting the attempt counter for the current attempt.

    Without this guard a delayed ghost can clobber run_task_helper_arn with its
    own dead ARN, causing future restarts' Stop_Old_Inner_Wrapper to target the
    wrong execution. A ConditionalCheckFailedException on the ghost must route
    to Stale_Attempt_Superseded, not continue execution."""
    state = template["States"]["Update_Status_Running"]
    cond = state["Arguments"].get("ConditionExpression", "")
    assert "attempt" in cond, (
        "Update_Status_Running must guard on 'attempt' to block ghost writes; "
        f"got ConditionExpression: {cond!r}"
    )
    assert ":expectedAttempt" in state["Arguments"]["ExpressionAttributeValues"], (
        "Update_Status_Running must bind :expectedAttempt for the ConditionExpression"
    )
    # Ghost-rejection path must route to Stale_Attempt_Superseded, not continue
    catches = {e: c["Next"] for c in state.get("Catch", []) for e in c["ErrorEquals"]}
    assert catches.get("DynamoDB.ConditionalCheckFailedException") == "Stale_Attempt_Superseded", (
        "ConditionalCheckFailedException from Update_Status_Running must go to "
        f"Stale_Attempt_Superseded; catch table: {catches}"
    )


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

WRAPPER_ARN = "arn:aws:states:us-east-1:111111111111:stateMachine:wrapper"


def _wrapper_input_for(build):
    """Build a one-task DAG via the real SDK and return the Input dict the
    pipeline ASL hands to run_task (contains the SDK-emitted task_config)."""
    from polyris import DAG
    from polyris.generators import _build_task_branch
    with DAG(dag_id="contract", schedule="@daily") as dag:
        build(dag)
    t = dag.tasks[0]
    branch = _build_task_branch(t, dag, WRAPPER_ARN)
    state = next(iter(branch["States"].values()))
    return state["Arguments"]["Input"]


def _resolve_arguments(template, state_name, wrapper_input):
    """Resolve a Run_Task_<X> state's Arguments JSONata with $states bound."""
    jsonata = pytest.importorskip("jsonata")

    def resolve(node):
        if isinstance(node, str) and node.startswith("{%") and node.endswith("%}"):
            expr = node[2:-2].strip()
            j = jsonata.Jsonata(expr)
            j.assign("states", {"input": wrapper_input})
            return j.evaluate({})
        if isinstance(node, dict):
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(template["States"][state_name]["Arguments"])


def test_emr_step_reaches_addstep(template):
    """@task.emr's emr_step must arrive intact as the addStep ``Step`` argument.
    Regression for the task_config schema mismatch (SDK emitted {cluster_id,
    step}; the wrapper read flat step_name/jar/... -> HadoopJarStep.Jar empty)."""
    from polyris import task
    jar = "s3://bucket/spark.jar"

    def build(dag):
        @task.emr(
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
    """@task.emr must reject a step missing the required HadoopJarStep.Jar at
    decoration time, not silently emit an invalid addStep call."""
    from polyris import DAG, task
    with DAG(dag_id="bad-emr", schedule="@daily"):
        with pytest.raises(ValueError, match="HadoopJarStep.Jar"):
            @task.emr(emr_cluster_id="j-ABC", emr_step={"Name": "x", "HadoopJarStep": {}})
            def step():
                pass


def test_emr_rejects_plural_steps():
    """Reject the RunJobFlow-style plural 'Steps' — addStep.sync takes one step."""
    from polyris import DAG, task
    with DAG(dag_id="bad-emr2", schedule="@daily"):
        with pytest.raises(ValueError, match="single StepConfig"):
            @task.emr(emr_cluster_id="j-ABC",
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
        @task.glue(job_name="etl", worker_type="G.2X", number_of_workers=10,
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
        @task.glue(job_name="etl")
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
            @task.glue(job_name="x", worker_type="G.1X", number_of_workers=2,
                       allocated_capacity=5)
            def j():
                pass


def test_glue_rejects_worker_type_without_count():
    from polyris import DAG, task
    with DAG(dag_id="bad-glue2", schedule="@daily"):
        with pytest.raises(ValueError, match="together"):
            @task.glue(job_name="x", worker_type="G.1X")
            def j():
                pass


# --- ecs: assign_public_ip must reach runTask; NetworkConfiguration conditional (G5) ---

def test_ecs_assign_public_ip_reaches_runtask(template):
    from polyris import task

    def build(dag):
        @task.ecs(cluster="c", task_definition="td:1", subnets=["subnet-1"],
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
        @task.ecs(cluster="c", task_definition="td:1", launch_type="EC2")
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
            @task.ecs(cluster="c", task_definition="td:1")  # FARGATE default, no subnets
            def e():
                pass


def test_glue_allocated_capacity_reaches_startjobrun(template):
    """allocated_capacity (the legacy DPU model, valid on its own) must reach
    startJobRun as AllocatedCapacity."""
    from polyris import task

    def build(dag):
        @task.glue(job_name="etl", allocated_capacity=8)
        def j():
            pass

    wi = _wrapper_input_for(build)
    resolved = _resolve_arguments(template, "Run_Task_Glue", wi)
    assert resolved["JobName"] == "etl"  # binding guard
    assert resolved.get("AllocatedCapacity") == 8, f"AllocatedCapacity dropped: {resolved!r}"
    assert "WorkerType" not in resolved


def test_emr_rejects_non_dict_step():
    from polyris import DAG, task
    with DAG(dag_id="bad-emr3", schedule="@daily"):
        with pytest.raises(ValueError, match="must be a dict"):
            @task.emr(emr_cluster_id="j-ABC", emr_step="not-a-dict")
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
        @task.lambda_(
            function_name="arn:aws:lambda:us-east-1:111111111111:function:fn",
            payload={"my_key": "v", "current_date": "SHOULD_NOT_WIN"},
        )
        def fn():
            pass

    sdk_input = _wrapper_input_for(build)
    # simulate the runtime $states.input after Prepare_Task_Input populated context
    runtime_input = {
        "task_arn": "arn:aws:lambda:us-east-1:111111111111:function:fn",
        "current_date": "2026-01-01",
        "PARTITION_ARG": "2026-01-01",
        "variables": {"myvar": "x"},
        "upstream": {"up": {"output": {"k": "v"}, "status": "success"}},
        "task_config": sdk_input["task_config"],
    }
    resolved = _resolve_arguments(template, "Run_Task_Lambda", runtime_input)
    payload = resolved["Payload"]

    assert payload["current_date"] == "2026-01-01"  # binding guard + orchestration wins
    assert payload.get("my_key") == "v"             # custom user key flows through
    assert payload["upstream"]["up"]["output"]["k"] == "v"  # XCom-in protected


# --- role: cross-account Credentials must apply to ALL wrapper-routed types (G6) ---

_WRAPPER_ROUTED_STATES = (
    "Run_Task_Lambda", "Run_Task_Glue", "Run_Task_ECS",
    "Run_Task_Athena", "Run_Task_EMR", "Run_Task_Batch",
)


def _resolve_role_arn(template, state_name, wrapper_input):
    jsonata = pytest.importorskip("jsonata")
    expr = template["States"][state_name]["Credentials"]["RoleArn"]
    j = jsonata.Jsonata(expr[2:-2].strip())
    j.assign("states", {"input": wrapper_input})
    return j.evaluate({})


def test_role_credentials_apply_to_all_wrapper_types(template):
    """An explicit cross-account role ARN must reach Credentials.RoleArn for
    every wrapper-routed service type, not only sfn (G6). The Credentials block
    reads $states.input.cross_account_role, which the SDK threads for all types."""
    from polyris import task
    arn = "arn:aws:iam::999999999999:role/cross"

    def build(dag):
        @task.glue(job_name="etl", role=arn)
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
        @task.lambda_(function_name="fn", role="same")
        def fn():
            pass

    wi = _wrapper_input_for(build)
    assert _resolve_role_arn(template, "Run_Task_Lambda", wi) == "0"


# --- retries: wrapper retry loop reads task_config (G7, ADR #107, option B) ---

def _eval_jsonata(expr, wrapper_input, variables=None):
    jsonata = pytest.importorskip("jsonata")
    body = expr[2:-2].strip() if expr.startswith("{%") else expr
    j = jsonata.Jsonata(body)
    j.assign("states", {"input": wrapper_input})
    for k, v in (variables or {}).items():
        j.assign(k, v)
    return j.evaluate({})


def test_retries_and_delay_reach_task_config():
    """task.retries / retry_delay must land in task_config so the wrapper loop
    can read them at runtime."""
    from datetime import timedelta
    from polyris import task

    def build(dag):
        @task.glue(job_name="etl", retries=3, retry_delay=timedelta(seconds=45))
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
        @task.glue(job_name="etl")
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
    """Wait_Before_Retry must wait task_config.retry_delay seconds (dynamic)."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    assert _eval_jsonata(secs, {"task_config": {"retry_delay": 45}}) == 45
    # default when unset
    assert _eval_jsonata(secs, {"task_config": {}}) == 0


def test_retry_loop_closes_back_to_dispatch(template):
    """The retry path must form a terminating loop: decision → wait → increment
    (bump counter) → back to Check_Task_Type to re-dispatch the same task."""
    states = template["States"]
    assert states["Check_Should_Retry"]["Choices"][0]["Next"] == "Wait_Before_Retry"
    assert states["Wait_Before_Retry"]["Next"] == "Increment_Retry"
    inc = states["Increment_Retry"]
    assert inc["Next"] == "Check_Task_Type"        # re-dispatch
    assert "retry_attempt" in inc["Assign"]        # counter bump => loop terminates


def test_decorators_reject_unknown_kwargs():
    """Variant decorators must fail fast on an unknown/typo'd parameter rather
    than silently swallowing it (D5 — **kwargs removed). Representative check."""
    from polyris import DAG, task
    with DAG(dag_id="strict", schedule="@daily"):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            @task.glue(job_name="x", job_nmae="typo")
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
        "lambda_": dict(function_name="fn"),
        "glue": dict(job_name="j"),
        "ecs": dict(cluster="c", task_definition="t:1", subnets=["subnet-x"]),
        "athena": dict(query_string="SELECT 1", database="db"),
        "emr": dict(
            emr_cluster_id="j-1",
            emr_step={"Name": "s", "HadoopJarStep": {"Jar": "command-runner.jar", "Args": ["x"]}},
        ),
        "batch": dict(job_definition="d:1", job_queue="q"),
    }
    with DAG(dag_id="assets-all-types", schedule="@daily"):
        for name, kw in specs.items():
            @getattr(task, name)(outlets=[produced], inlets=[consumed], wait_for=[consumed], **kw)
            def _t():
                pass
            assert _t.outlets == [produced], f"task.{name} dropped outlets"
            assert _t.inlets == [consumed], f"task.{name} dropped inlets"
            assert _t.wait_for == [consumed], f"task.{name} dropped wait_for"


# --- athena: full param parity + workgroup-managed output nuance ---

def test_athena_params_reach_query_execution(template):
    from polyris import task

    def build(dag):
        @task.athena(query_string="SELECT 1", database="analytics",
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
        @task.athena(query_string="SELECT 1", database="db", output_location="s3://r/")
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
        @task.athena(query_string="SELECT 1", database="db")  # workgroup-managed output
        def q():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Athena", wi)
    assert "ResultConfiguration" not in r, f"empty OutputLocation leaked: {r!r}"


# --- batch: full param parity ---

def test_batch_params_reach_submit_job(template):
    from polyris import task

    def build(dag):
        @task.batch(job_definition="jd:1", job_queue="jq", batch_parameters={"k": "v"})
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
        @task.batch(job_definition="jd:1", job_queue="jq")
        def b():
            pass

    wi = _wrapper_input_for(build)
    r = _resolve_arguments(template, "Run_Task_Batch", wi)
    assert r["Parameters"] == {}


# --- ecs: tighten — Overrides / LaunchType / SecurityGroups also flow ---

def test_ecs_overrides_launchtype_securitygroups_reach_runtask(template):
    from polyris import task

    def build(dag):
        @task.ecs(cluster="c", task_definition="td:1", subnets=["s-1"],
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
        @task.lambda_(function_name="my-fn")
        def f():
            pass

    wi = _wrapper_input_for(build)
    # evaluate only FunctionName: the Payload $merge needs a runtime-resolved
    # `variables` object (Prepare_Task_Input builds it), covered separately.
    fn_expr = template["States"]["Run_Task_Lambda"]["Arguments"]["FunctionName"]
    assert _eval_jsonata(fn_expr, wi) == "my-fn"


# --- exponential backoff (ADR #107, opt-in via retry_exponential_backoff) ---

def test_retry_wait_exponential_backoff_when_enabled(template):
    """With retry_backoff the wait doubles per attempt, capped at max_retry_delay."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True, "max_retry_delay": 60}}
    assert _eval_jsonata(secs, tc, {"retry_attempt": 0}) == 10   # 10 * 2^0
    assert _eval_jsonata(secs, tc, {"retry_attempt": 1}) == 20   # 10 * 2^1
    assert _eval_jsonata(secs, tc, {"retry_attempt": 2}) == 40   # 10 * 2^2
    assert _eval_jsonata(secs, tc, {"retry_attempt": 3}) == 60   # 80 -> capped at 60
    assert _eval_jsonata(secs, tc, {"retry_attempt": 9}) == 60   # stays capped


def test_retry_wait_backoff_uncapped_defaults_to_ceiling(template):
    """Backoff without max_retry_delay still bounds the wait at the 3600s default."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True}}  # no cap set
    assert _eval_jsonata(secs, tc, {"retry_attempt": 2}) == 40
    assert _eval_jsonata(secs, tc, {"retry_attempt": 20}) == 3600   # default ceiling


def test_retry_wait_fixed_when_backoff_disabled(template):
    """Without retry_backoff the wait stays fixed regardless of attempt (default)."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 10}}
    assert _eval_jsonata(secs, tc, {"retry_attempt": 0}) == 10
    assert _eval_jsonata(secs, tc, {"retry_attempt": 5}) == 10


def test_backoff_config_threaded_only_when_enabled():
    """retry_backoff + max_retry_delay reach task_config only when opted in."""
    from datetime import timedelta
    from polyris import task

    def build_on(dag):
        @task.glue(job_name="etl", retries=3, retry_delay=timedelta(seconds=10),
                   retry_exponential_backoff=True, max_retry_delay=timedelta(seconds=60))
        def j():
            pass

    tc = _wrapper_input_for(build_on)["task_config"]
    assert tc["retry_backoff"] is True
    assert tc["max_retry_delay"] == 60

    def build_off(dag):
        @task.glue(job_name="etl", retries=3, retry_delay=timedelta(seconds=10))
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
        @task.glue(job_name="etl", retries=2, retry_delay=timedelta(seconds=5),
                   retry_exponential_backoff=True)  # no max_retry_delay
        def j():
            pass

    tc = _wrapper_input_for(build)["task_config"]
    assert tc["retry_backoff"] is True
    assert "max_retry_delay" not in tc


# --- retry jitter (ADR #107, opt-in equal jitter via $random) ---

def test_retry_wait_jitter_bounds_over_backoff(template):
    """With retry_jitter over exponential backoff, each wait is a random integer in
    [base/2, base) where base = min(retry_delay*2^attempt, cap)."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True,
                          "max_retry_delay": 60, "retry_jitter": True}}
    vals = [_eval_jsonata(secs, tc, {"retry_attempt": 2}) for _ in range(200)]  # base=40
    assert all(float(v).is_integer() for v in vals)
    assert all(20 <= v < 40 for v in vals), (min(vals), max(vals))
    assert len(set(vals)) > 1  # genuinely randomised, not a constant


def test_retry_wait_jitter_bounds_over_fixed(template):
    """Jitter also applies over a fixed (non-backoff) delay: [base/2, base)."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 45, "retry_jitter": True}}
    vals = [_eval_jsonata(secs, tc, {"retry_attempt": 3}) for _ in range(200)]
    assert all(22 <= v < 45 for v in vals), (min(vals), max(vals))


def test_retry_wait_no_jitter_is_deterministic(template):
    """Without retry_jitter the wait is exactly the computed value (no randomness)."""
    secs = template["States"]["Wait_Before_Retry"]["Seconds"]
    tc = {"task_config": {"retry_delay": 10, "retry_backoff": True, "max_retry_delay": 60}}
    vals = {_eval_jsonata(secs, tc, {"retry_attempt": 2}) for _ in range(20)}
    assert vals == {40}


def test_jitter_config_threaded_only_when_enabled():
    """retry_jitter reaches task_config only when opted in (works with or without backoff)."""
    from datetime import timedelta
    from polyris import task

    def build_on(dag):
        @task.glue(job_name="etl", retries=2, retry_delay=timedelta(seconds=10),
                   retry_jitter=True)
        def j():
            pass

    assert _wrapper_input_for(build_on)["task_config"]["retry_jitter"] is True

    def build_off(dag):
        @task.glue(job_name="etl", retries=2, retry_delay=timedelta(seconds=10))
        def j():
            pass

    assert "retry_jitter" not in _wrapper_input_for(build_off)["task_config"]
