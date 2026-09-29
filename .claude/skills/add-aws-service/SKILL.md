---
name: add-aws-service
description: Use when integrating a new AWS service into the Polyris stack beyond the ones already wired (Step Functions, Lambda, DynamoDB, EventBridge, SNS, SQS, S3, Glue, Athena, ECS, EMR, Batch, SageMaker, Bedrock, HTTP) — e.g. Kinesis, Redshift, a new compute target or data source. Polyris is a DSL→ASL compiler, so a first-class service is **primarily a library change** — a new `TaskTypeLiteral`, a `@task.<svc>` decorator (`polyris/task.py`), and a codegen branch (`polyris/generators.py`) — and only then SAM/IAM, cost, an ADR, and tests (ASL snapshots). Covers the SFN-first principle, the first-class-task vs inside-a-Lambda decision, and the full test surface. Trigger for "add/integrate <service>", "use <AWS service>", "new task type", or "new data source/sink".
---

# Integrating a new AWS service

Polyris compiles a Python DSL → Amazon States Language → Step Functions. "Adding a service" is
usually **a library change first** (a new task type the DSL emits), then infra (SAM/IAM). Every
service is standing cost + operational surface — read the cost and SFN-direction ADRs in
`docs/reference/DESIGN_DECISIONS.md` first. The stack is deliberately small (~$31/month).

## Step 1 — Fit + pick the integration shape

Orchestration is **Step-Functions-first** (the async push-model, Phase 3, is shelved — poor ROI, hurts debuggability). Two shapes, and the shape decides whether you touch the library:

- **First-class SFN task** — `@task.<service>(...)` compiles to a native SFN service integration
  (`arn:aws:states:::<service>:<action>[.sync]`). This is the dominant existing pattern (glue, ecs,
  athena, sns, sqs, s3, dynamodb, eventbridge, bedrock, http). **Requires the library work in Step 2.**
- **Inside a Lambda** — the user's `@task.lambda_function` / python function calls the service via
  boto3. **No library change** — only an IAM grant on the Lambda role (Step 3). Skip Step 2.

Prefer serverless + on-demand + Express SFN over always-on infra. If the service implies a
fundamentally different orchestration model, stop and write the ADR first — that is an architecture
decision, not a wiring task.

## Step 2 — The library: new task type + codegen (first-class shape)

Mirror `glue` end-to-end — it is the cleanest reference. Touch points, in order:

1. **`polyris/constants.py`** — add the value to `TaskTypeLiteral`. Task types are Python-only —
   not part of enum-sync codegen, so no `make sync-constants`.
2. **`polyris/task.py`** — add service config fields to the `Task` dataclass; add a
   `@task.<service>(...)` classmethod to `TaskDecorator` (mirror `TaskDecorator.glue` / `.ecs`):
   declare only service-specific params explicitly; take shared params via
   `**common: Unpack[CommonTaskKwargs]`, calling `_validate_common_kwargs("<service>", common)` first
   (ADR #109). A **new common parameter** goes into `CommonTaskKwargs` + `_create_task` once —
   never per-decorator. Assets (`outlets`/`inlets`/`wait_for`) are already common — do not
   re-declare them. Add the new decorator to the "base `@task` is not allowed" error list.
3. **`polyris/generators.py`** — two dispatch sites, keep in sync. The split (dict + `if/elif`) is
   intentional for now — the registry refactor (paid emitters self-register without editing the free
   compiler) is deferred until the first paid AWS service. Until then, edit both sites together.
   - Write `_gen_<service>_state(step)` returning the SFN Task state with the service ARN, and
     register it in the state-generator **dispatch dict**.
   - Add an `elif task.task_type == '<service>':` branch to the `task_config` builder in
     `_build_task_branch`. Keys are **never bare strings** — declare each as a
     `polyris.constants.TaskConfigKey` member first (ADR #108); `tests/sdk/test_task_config_contract.py`
     fails on any literal on either side.
   - Add the type to `WRAPPER_STEP_TYPES` / `TRACKED_STEP_TYPES` if it should get the
     started/failed wrapper and appear in DAG visualization.
4. **`polyris/adapters/<service>.py`** — only if it is a data source feeding the asset model
   (schema / DDL / partition inference). A pure compute target needs no adapter.

> **The `task_config` ↔ `Run_Task_<X>` contract.** Wrapper-routed types
> (`lambda`/`glue`/`ecs`/`athena`/`emr`/`batch`) compile to a `waitForTaskToken` call into the
> shared `run_task` wrapper (`sam/sfn_templates/helpers/run_task/sfn.tpl.json`), which routes on
> `task_type` to a `Run_Task_<X>` state that reads `task_config` via JSONata. The keys your
> `task_config` builder writes **must match exactly** what `Run_Task_<X>.Arguments` reads — a
> mismatch fails at runtime in AWS, not at build time. This is how the emr integration silently
> broke: `HadoopJarStep.Jar` came out empty. Edit **both** the builder *and* its `Run_Task_<X>`
> state, and pin them with a contract test (Step 8). Emit a service sub-block conditionally when
> an empty value would make the AWS call invalid (e.g. ecs `NetworkConfiguration` only with
> subnets). `sfn` is the exception — it carries no `task_config`.

## Step 3 — SAM template + IAM

1. Grant **least-privilege** IAM for the integration: specific `<service>:…` actions on the
   **SFN execution role** (first-class) or the **Lambda role** (inside-Lambda).
2. Add the resource (`AWS::SQS::Queue`, etc.) only if Polyris owns it, with sane defaults
   (encryption, retention, DLQ).
3. Pass ARNs via `!Ref` / `!GetAtt` as env vars or SFN parameters — never hard-coded.
4. Keep `sam build` working without `--use-container`: pure-Python deps only (ADR #65).

## Step 4 — Stateful resource safety

If the service stores state you can't lose: `DeletionPolicy: Retain` + `UpdateReplacePolicy: Retain`,
and for DynamoDB `PointInTimeRecoverySpecification`.

## Step 5 — Cost check

State the monthly delta before merging (requests × price + any always-on baseline + storage). If it
materially moves ~$31/month, justify it in the ADR. On-demand / pay-per-use shapes are preferred.

## Step 6 — Record the decision (ADR)

Add a standalone `docs/reference/adr-<N>-<slug>.md` (next number after the current ceiling; mirror
`adr-106`/`adr-107`). Capture: what the service is for, why it vs alternatives, cost delta, and
orchestration shape.

## Step 7 — Surfaces that follow

- **Backend** route(s) to expose its data/status — add a self-registering route module under
  `sam/lambdas/console_api/` (ADR #97).
- **UI** view/panel under `ui/`.

## Step 8 — Tests

- **ASL snapshot** (`tests/sdk/test_asl_snapshots_steps.py`) — add a `_build_…` DAG builder and a
  `test_snapshot_step_*` to pin the emitted `arn:aws:states:::<service>:…` state. Mirror
  glue/ecs/athena builders.
- **Wrapper contract** (`tests/sdk/test_run_task_template.py`) — **required for wrapper-routed types.**
  Resolve the new `Run_Task_<X>.Arguments` JSONata against the exact `task_config` the SDK emits and
  check every parameter arrives. This is the test that would have caught the emr break.
- **Decorator / Task** (`tests/sdk/`) — `@task.<service>(…)` produces the right `Task` and rejects
  missing required params. Your new type is automatically swept by
  `test_every_task_decorator_accepts_common_kwargs` and `test_all_task_types_wire_assets`.
- **Adapter** (if added) — a `tests/sdk/test_adapters_<service>.py` mirroring `test_adapters_glue.py`.
- **Integration** (`tests/integration/`) for the real AWS shape.

Run the SDK/codegen suite while iterating: `pip install -e . && pytest tests/sdk/ -v`.

## Step 9 — Verify

`make check` (lint + sync-constants + check-versions + smoke-pipelines + tests) plus `cfn-lint` on
the template. Confirm the SFN definition still validates and the Express/Standard split is intact.
