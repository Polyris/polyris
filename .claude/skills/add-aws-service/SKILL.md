---
name: add-aws-service
description: Use when integrating a new AWS service into the Polyris stack beyond the ones already wired (Step Functions, Lambda, DynamoDB, EventBridge, SNS, SQS, S3, Glue, Athena, ECS, EMR, Batch, SageMaker, Bedrock, HTTP) — e.g. Kinesis, Redshift, a new compute target or data source. Polyris is a DSL→ASL compiler, so a first-class service is **primarily a library change** — a new `TaskTypeLiteral`, a `@task.<svc>` decorator (`polyris/task.py`), and a codegen branch (`polyris/generators.py`) — and only then SAM/IAM, cost, an ADR, and tests (ASL snapshots). Covers the SFN-first principle, the first-class-task vs inside-a-Lambda decision, and the full test surface. Trigger for "add/integrate <service>", "use <AWS service>", "new task type", or "new data source/sink".
---

# Integrating a new AWS service

Polyris compiles a Python DSL → Amazon States Language → Step Functions. So
"adding a service" is usually **a library change first** (a new task type the DSL
emits), then infra (SAM/IAM). It is also deliberately small and cheap
(~$31/month) — every service is standing cost + operational surface. Read the
cost and SFN-direction ADRs in `docs/reference/DESIGN_DECISIONS.md` first.

## Step 1 — Fit + pick the integration shape

Orchestration is **Step-Functions-first** (the async push-model, Phase 3, is
shelved — poor ROI, hurts debuggability). Two shapes, and the shape decides
whether you touch the library:

- **First-class SFN task** — `@task.<service>(...)` compiles to a native SFN
  service integration (`arn:aws:states:::<service>:<action>[.sync]`). This is the
  dominant existing pattern (glue, ecs, athena, sns, sqs, s3, dynamodb,
  eventbridge, bedrock, http). **Requires the library work in Step 2.**
- **Inside a Lambda** — the user's `@task.lambda_` / python function calls the
  service via boto3. **No library change** — only an IAM grant on the Lambda role
  (Step 3), plus a SAM resource if Polyris owns it. Skip Step 2.

Prefer serverless + on-demand + Express SFN over always-on infra; a
provisioned/clustered mode must be justified against the cost discipline. If the
service implies a fundamentally different orchestration model, stop and write the
ADR first (Step 6) — that is an architecture decision, not a wiring task.

## Step 2 — The library: new task type + codegen (first-class shape)

Mirror an existing service end-to-end — `glue` is the cleanest reference. Touch
points, in order:

1. **`polyris/constants.py`** — add the value to the `TaskTypeLiteral` literal.
   (Task types are Python-only — they are **not** part of the enum-sync codegen,
   so no `make sync-constants` for this.)
2. **`polyris/task.py`** —
   - add the service's config fields to the `Task` dataclass (mirror
     `glue_arguments`, `emr_step`, `batch_parameters`);
   - add a `@task.<service>(...)` classmethod to `TaskDecorator` (mirror
     `TaskDecorator.glue` / `.ecs`): declare **only the service-specific
     params** explicitly (required keyword args; structural checks raise
     `ValueError` — this is the real "validation") and take the shared common
     params via `**common: Unpack[CommonTaskKwargs]`, calling
     `_validate_common_kwargs("<service>", common)` before the
     `self._create_task(..., **common)` forward (ADR #109). A **new common
     parameter** is added once — to `CommonTaskKwargs` + `_create_task`
     (+ the `Task` field) — never per-decorator. (Assets —
     `outlets`/`inlets`/`wait_for` — are common params for exactly this reason:
     every task type gets them via `**common`, and the generator handles them
     generically. Do **not** re-declare them on your decorator.)
     `test_every_task_decorator_accepts_common_kwargs` fails any decorator that
     drops `**common`. Typos still fail fast: the validator raises `TypeError`
     naming the decorator;
   - add the new decorator to the "base `@task` is not allowed" error list.
3. **`polyris/generators.py`** — the codegen, in **two** dispatch sites (keep them
   in sync):
   - write `_gen_<service>_state(step)` returning
     `{"Type":"Task","Resource":"arn:aws:states:::<service>:<action>[.sync]","Arguments":{…}}`
     (mirror `_gen_glue_state`), and register it in the state-generator
     **dispatch dict**;
   - add an `elif task.task_type == '<service>':` branch to the per-type
     `task_config` builder in `_build_task_branch`. **This `task_config` is not
     just metadata — it is the runtime contract the wrapper reads** (see the
     contract note below);
   - add the type to `WRAPPER_STEP_TYPES` / `TRACKED_STEP_TYPES` if it
     should get the started/failed wrapper and appear in DAG visualization.
   > **Deferred-refactor note:** the dispatch is split across the dict and the
   > `if/elif` on purpose-for-now. The `generators.py` registry refactor (so paid
   > emitters self-register without editing the free compiler) is deferred until
   > the **first paid** AWS service — its own stage + ADR. Until then, edit both
   > sites together.
4. **`polyris/adapters/<service>.py`** — **only if it is a data source** that
   feeds the asset model (schema / DDL / partition inference), like
   `adapters/glue.py`. A pure compute target needs no adapter.

> **The `task_config` ↔ `Run_Task_<X>` contract — read this; it is how the emr
> integration silently broke.** Wrapper-routed types
> (`lambda`/`glue`/`ecs`/`athena`/`emr`/`batch`) do **not** run their
> `_gen_<service>_state` directly in the pipeline — they compile to a
> `waitForTaskToken` call into the shared `run_task` wrapper
> (`sam/sfn_templates/helpers/run_task/sfn.tpl.json`), which routes on `task_type`
> to a `Run_Task_<X>` state that reads `task_config` and calls the service. So the
> keys your `task_config` builder writes **must match exactly** what
> `Run_Task_<X>.Arguments` reads — a mismatch fails at runtime in AWS, not at build
> time. Keys are **never bare strings**: declare each as a
> `polyris.constants.TaskConfigKey` member first and key the builder dict with
> the enum (ADR #108) — `tests/sdk/test_task_config_contract.py` fails on any
> literal on either side (emr wrote `{cluster_id, step}` while the wrapper read flat
> `step_name`/`jar`/…, so `HadoopJarStep.Jar` came out empty). Adding a service
> means editing **both** the builder *and* its `Run_Task_<X>` state, and **pinning
> them together** with a contract test (Step 8). Emit a service sub-block
> **conditionally** when an empty value would make the AWS call invalid (e.g. ecs
> `NetworkConfiguration` only with subnets, athena `ResultConfiguration` only with
> an output location). `sfn` is the exception — it carries no `task_config`
> (`Run_Task_SFN` uses `task_arn` directly). See ADR #106.

## Step 3 — SAM template + IAM

In `sam/template.yaml`:

1. Grant **least-privilege** IAM for the integration: the specific `<service>:…`
   actions on the **SFN execution role** (first-class task) or the **Lambda role**
   (inside-a-Lambda). Follow the existing role policies; do not widen a broad
   statement.
2. Add the resource (`AWS::SQS::Queue`, `AWS::SNS::Topic`, …) only if Polyris owns
   it, with sane defaults (encryption, retention, DLQ).
3. Pass ARNs via `!Ref` / `!GetAtt` as env vars or SFN parameters — never
   hard-coded.

Keep `sam build` working without `--use-container`: pure-Python deps only
(ADR #65). No native-wheel libraries in `console_api/requirements.txt`.

## Step 4 — Stateful resource safety

If the service stores state you can't lose (a new table, bucket, queue):
`DeletionPolicy: Retain` + `UpdateReplacePolicy: Retain`, and for DynamoDB
`PointInTimeRecoverySpecification`.

## Step 5 — Cost check

State the monthly delta before merging (requests × price + any always-on baseline
+ storage). If it materially moves ~$31/month, justify it in the ADR. On-demand /
pay-per-use shapes are preferred.

## Step 6 — Record the decision (ADR)

Add a standalone `docs/reference/adr-<N>-<slug>.md` (next number after the current
ceiling; mirror `adr-106`/`adr-107`). Capture: what the service is for, why it vs
alternatives, the cost delta, and the orchestration shape (how it fits SFN-first).

## Step 7 — Surfaces that follow

- **Backend** route(s) to expose its data/status — add a self-registering route
  module under `sam/lambdas/console_api/` (see ADR #97 for the registration
  pattern, and CONTRIBUTING.md).
- **UI** to view it — add a view/panel in the Next.js console under `ui/`.

## Step 8 — Tests

- **ASL snapshot** (`tests/sdk/test_asl_snapshots_steps.py`) — the primary codegen
  test: add a `_build_…` DAG builder and a `test_snapshot_step_*` so the emitted
  `arn:aws:states:::<service>:…` state is pinned. Mirror the glue/ecs/athena
  builders already there.
- **Wrapper contract** (`tests/sdk/test_run_task_template.py`) — **required for
  wrapper-routed types.** Resolve the new `Run_Task_<X>.Arguments` JSONata against
  the exact `task_config` the SDK emits (with `$states` bound — assert a known
  field as a binding guard) and check every parameter arrives. This is the test
  that would have caught the emr break. Mirror the glue/ecs/emr contract tests.
- **Decorator / Task** (`tests/sdk/`, e.g. `test_schema.py`) — `@task.<service>(…)`
  produces the right `Task` and rejects missing required params. Your new type is
  automatically swept by `test_every_task_decorator_accepts_common_kwargs` and
  `test_all_task_types_wire_assets` in `test_run_task_template.py` (they discover
  every `@task.*`) — if either fails, your decorator dropped `**common`.
- **Template drift** (`tests/sdk/test_sfn_template_drift.py`) stays green.
- **Adapter** (if added) — a `tests/sdk/test_adapters_<service>.py` mirroring
  `test_adapters_glue.py`.
- **Integration** (`tests/integration/`) for the real AWS shape; `pytest-mock`
  (ADR #26) at the boundary for unit.

Run the SDK/codegen suite directly while iterating:
`pip install -e . && pytest tests/sdk/ -v`.

## Step 9 — Verify

`make check` (lint + sync-constants + check-versions + smoke-pipelines + tests)
plus `cfn-lint` on the template. Confirm the SFN definition still validates and
the Express/Standard split is intact.
