# polyris Python DSL Reference

## Contents

- [Overview](#overview)
- [How polyris relates to AWS](#how-polyris-relates-to-aws) — decorators orchestrate existing resources
- [DAG Definition](#dag-definition)
- [Schedule Options](#schedule-options)
- [Task Types](#task-types) — `sfn`, `lambda_function`, `glue_job`, `ecs_task`, `athena_query`, `emr_step`, `batch_job`
- [Common parameters](#common-parameters-apply-to-every-task-type) — retry, timeout, trigger rule, role, assets
- [Task-type-specific parameters](#task-type-specific-parameters)
- [Trigger Rules](#trigger-rules)
- [Dependencies](#dependencies)
- [Assets](#assets)
- [Variables](#variables)
- [Complete Example](#complete-example)
- [CLI Usage](#cli-usage)

## Overview

polyris provides a Python DSL for defining data pipelines that compile to AWS
Step Functions. Use `@task` decorators, the `>>` dependency operator, and a
`DAG()` context manager to describe pipelines — they execute as Step Functions
state machines, with no scheduler or worker pool to operate.

> Passing data between tasks is a separate topic — see
> [DATA_PASSING.md](DATA_PASSING.md) for `xcom.pull`, upstream auto-injection,
> and how each task type reads its predecessor's output.

## How polyris relates to AWS

**Every `@task.<service>` decorator orchestrates an AWS resource that already
exists.** polyris does not provision the state machine, function, job, cluster,
or database on your behalf — it only wires the deploy / trigger / retry /
monitor logic.

Before you write:

```python
@task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:my-workflow")
```

that Step Function state machine must already be in your AWS account, created
by whatever provisioning tool you use (SAM, CDK, Terraform, hand-created).
Same for Lambda functions, Glue jobs, ECS task definitions, Athena databases,
EMR clusters, and Batch job definitions/queues — polyris expects them to be
there, and calls them by their AWS identifier.

The one thing polyris deploys is your pipeline itself (a set of Step Function
state machines that glue your existing resources together into a DAG).

### `namespace` is a resource-naming prefix, not a DSL concept

`namespace` is set once per deployment in `pipelines/config.py` (or via the SAM
`Namespace` parameter). It prefixes every AWS resource polyris creates (S3
bucket, DDB tables, IAM roles) so multiple polyris installs in the same account
don't collide. It **does not have to match** `Stage`, `dag_id`, or anything
inside your DSL. See `docs/reference/CONFIGURATION.md` for the full lifecycle.

Your `@task.sfn(arn=...)` values point at whatever ARNs you actually have in
AWS — they're never derived from `namespace`.

---

## DAG Definition

```python
from polyris import DAG, task, Asset

with DAG(
    dag_id="my-pipeline",
    schedule="@daily",
    description="My data pipeline",
    tags=["production", "etl"],
    variables={
        "custom_var": "value"
    }
) as dag:
    ...
```

### DAG Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dag_id` | str | Yes | Unique pipeline identifier |
| `schedule` | str/Asset/list | No | Schedule: cron, preset, or assets |
| `description` | str | No | Human-readable description |
| `tags` | list | No | Tags for organization |
| `variables` | dict | No | Pipeline variables |
| `max_active_tasks` | int | No | Max concurrent tasks (default: 16) |
| `default_args` | dict | No | Default params for all tasks (see below) |

### Default Args

Apply shared parameters to all tasks in a DAG:

```python
from datetime import timedelta

with DAG(
    "my-pipeline",
    schedule="@daily",
    default_args={
        "retries": 2,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),
        "orchestration_timeout": timedelta(hours=12),  # Wait for deps up to 12h
    }
) as dag:
    # All tasks inherit these defaults unless overridden
    @task.sfn(arn=..., retries=0)  # Override: no retries for this task
    def fragile_task(): pass
```

### Alerts Configuration

> **Removed (ADR #103).** The `alerts=` argument has been removed — passing
> `alerts={...}` now raises a `TypeError`. Remove it from your DAGs. Alert
> delivery moved out of the DSL:
>
> - **Browser notifications** (in-app) are automatic and free — no setup.
> - Alert config is not part of the DSL (ADR #103) — there is no `alerts=`
>   argument.
>
> `DAG` has no `alerts=` argument (ADR #103) — passing `alerts={...}` now raises
> a `TypeError`. Configure alerts in Settings → Alerts instead.

## Schedule Options

### Time-Based (EventBridge)

```python
# Presets
DAG(schedule="@daily")      # cron(0 0 * * ? *)
DAG(schedule="@hourly")     # cron(0 * * * ? *)
DAG(schedule="@weekly")     # cron(0 0 ? * SUN *)

# Custom cron
DAG(schedule="cron(0 8 * * ? *)")   # 8:00 UTC daily

# Rate expressions
DAG(schedule="rate(6 hours)")
DAG(schedule="rate(1 day)")
```

### Asset-Based (Cross-Pipeline Triggers)

```python
from polyris import Asset
from polyris.assets import AssetAny

asset_a = Asset("processed/acme")
asset_b = Asset("processed/ulta")

# Single asset — AND-of-one: any ONE materialization satisfies it, but the
# trigger itself is deduplicated per calendar day (a producer that
# materializes asset_a more than once on the same day only triggers this
# consumer once — see docs/reference/SPIKE_ASSET_TRIGGER_GRANULARITY.md).
DAG(schedule=asset_a)

# Multiple assets - ALL required (AND, also day-deduplicated)
DAG(schedule=[asset_a & asset_b])

# Multiple assets - ANY triggers (OR) — fires on every materialization of
# either asset, no day-scoped dedup at all.
DAG(schedule=[asset_a | asset_b])

# Single asset, but fire on EVERY materialization (no day-scoped dedup) —
# explicit AssetAny with one item, rather than the bare-asset shorthand
# above. Use this for a producer that runs more than once a day by design
# (hourly, etc.) where every run should recompute the consumer.
DAG(schedule=AssetAny([asset_a]))
```

### Manual Only

```python
DAG(schedule=None)  # No automatic trigger
```

---

## Task Types

Seven decorators are shipped. Each one orchestrates a specific AWS service. All
of them accept the shared **common parameters** (retry / timeout / trigger rule
/ role / assets — see [Common parameters](#common-parameters-apply-to-every-task-type)
below) on top of their type-specific arguments.

### `@task.sfn` — AWS Step Functions

Executes a nested Step Function state machine.

**Required:**
- `arn` (str) — Full ARN of the state machine to invoke.

**Type-specific (optional):** none.

```python
@task.sfn(
    arn="arn:aws:states:us-east-1:123456789:stateMachine:my-sfn",
    # Any common parameter is welcome here too:
    retries=2,
    trigger_rule="all_success",
)
def my_task():
    pass
```

### `@task.lambda_function` — AWS Lambda

Invokes an existing Lambda function directly (not via a nested Step Function).

**Required (one of):**
- `function_name` (str) — Function name in the current region.
- `arn` (str) — Full function ARN (use this for cross-region / cross-account).

**Type-specific (optional):**
- `payload` (dict) — JSON input passed to the Lambda `event`.

```python
@task.lambda_function(
    function_name="my-function",
    payload={"key": "value"},
)
def process_data():
    pass
```

### `@task.glue_job` — AWS Glue

Starts an existing Glue job run.

**Required:**
- `job_name` (str) — Glue job name.

**Type-specific (optional):**
- `glue_arguments` (dict[str, str]) — `--key value` overrides for the job.
- `worker_type` + `number_of_workers` — must be set together (`G.1X` / `G.2X` / etc). For PySpark (glueetl) jobs.
- `max_capacity` (float) — DPU allocation for Python Shell jobs. Supports fractional values (e.g. `0.0625` for 1/16 DPU). Mutually exclusive with `worker_type`/`number_of_workers`.
- `allocated_capacity` (int) — Integer DPU model (deprecated by AWS). Use `max_capacity` instead — it supports the same whole-DPU values and additionally allows fractional values.
- `command_name` (str) — `"pythonshell"` or `"glueetl"`. Enables cross-type validation (e.g. rejects `worker_type` on a Python Shell job).

```python
# PySpark job
@task.glue_job(
    job_name="my-etl-job",
    glue_arguments={"--date": "2024-01-01"},
    worker_type="G.1X",
    number_of_workers=2,
    command_name="glueetl",  # optional — enables cross-type validation
)
def etl_job():
    pass

# Python Shell with 1/16 DPU
@task.glue_job(job_name="light-job", max_capacity=0.0625, command_name="pythonshell")
def light_job():
    pass
```

### `@task.ecs_task` — Amazon ECS (Fargate or EC2)

Runs an existing ECS task definition.

**Required:**
- `cluster` (str) — ECS cluster name.
- `task_definition` (str) — Task definition name (`name` or `name:revision`).

**Type-specific (optional):**
- `launch_type` (str, default `"FARGATE"`) — `FARGATE` or `EC2`.
- `subnets` (list[str]) — **Required if `launch_type="FARGATE"`** (Fargate runs
  in an ENI).
- `security_groups` (list[str]).
- `assign_public_ip` (str, `"ENABLED"` / `"DISABLED"`).
- `container_overrides` (dict) — Passed through to `RunTask`.

```python
@task.ecs_task(
    cluster="my-cluster",
    task_definition="my-task:1",
    launch_type="FARGATE",
    subnets=["subnet-xxx"],
    security_groups=["sg-xxx"],
    container_overrides={
        "containerOverrides": [{
            "name": "main",
            "command": ["python", "script.py"],
        }],
    },
)
def container_job():
    pass
```

`NetworkConfiguration` is sent only when `subnets` are provided; EC2 launch type
with `bridge` / `host` networking can omit them.

### `@task.athena_query` — Amazon Athena

Runs a SQL query against an existing Athena database.

**Required:**
- `query_string` (str) — SQL to execute.
- `database` (str) — Athena database.

**Type-specific (optional):**
- `output_location` (str) — S3 URL for results. Omit to use the workgroup's
  enforced output location.
- `workgroup` (str, default `"primary"`).

```python
@task.athena_query(
    query_string="SELECT * FROM my_table",
    database="my_database",
    output_location="s3://bucket/output/",
    workgroup="primary",
)
def run_query():
    pass
```

Omitting `output_location` makes the wrapper skip `ResultConfiguration` entirely
rather than sending an empty `OutputLocation` (which `StartQueryExecution`
rejects).

### `@task.emr_step` — Amazon EMR

Adds a single step to an existing EMR cluster (`elasticmapreduce:addStep`).

**Required:**
- `emr_cluster_id` (str) — Existing EMR cluster ID (`j-XXXX...`).
- `emr_step` (dict) — AWS `StepConfig` dict. Must contain `HadoopJarStep.Jar`.

**Type-specific (optional):** none.

```python
@task.emr_step(
    emr_cluster_id="j-XXXXXXXXXXXXX",
    emr_step={
        "Name": "Spark Job",
        "ActionOnFailure": "CONTINUE",
        "HadoopJarStep": {
            "Jar": "command-runner.jar",
            "Args": ["spark-submit", "s3://bucket/script.py"],
        },
    },
)
def spark_job():
    pass
```

### `@task.batch_job` — AWS Batch

Submits an existing Batch job definition to a job queue.

**Required:**
- `job_definition` (str) — Batch job definition (`name` or `arn`).
- `job_queue` (str) — Batch job queue (`name` or `arn`).

**Type-specific (optional):**
- `batch_parameters` (dict[str, str]) — Substitution parameters passed to
  `SubmitJob`.

```python
@task.batch_job(
    job_definition="my-job-def",
    job_queue="my-queue",
    batch_parameters={"param1": "value1"},
)
def batch_job():
    pass
```

---

## Common parameters (apply to every task type)

Every `@task.<service>` accepts these — they control retry / timeout / trigger
policy and are the same across `sfn`, `lambda_function`, `glue_job`,
`ecs_task`, `athena_query`, `emr_step`, and `batch_job`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task_id` | str | (inferred from function name) | Override the auto-inferred task id |
| `execution_timeout` | timedelta | 24 hours | Max task execution time |
| `orchestration_timeout` | timedelta | same as `execution_timeout` | Max time waiting for dependencies |
| `retries` | int | 0 | Number of retry attempts |
| `retry_delay` | timedelta | 5 minutes | Delay between retries (base delay when backoff is on) |
| `retry_exponential_backoff` | bool | False | Double the wait each retry: `min(retry_delay·2^n, max_retry_delay)` |
| `max_retry_delay` | timedelta | none (3600s cap) | Ceiling for exponential backoff |
| `retry_jitter` | bool | False | Randomise each wait into `[base/2, base)` (avoids retry stampede) |
| `wait_before` | int (seconds) | 0 | Wait N seconds before executing (rate limiting) |
| `trigger_rule` | str | `"all_success"` | Condition on **declared direct upstream** deps — see [Trigger Rules](#trigger-rules) below |
| `role` | str | `"same"` | Cross-account role key from `config.py` roles dict (`'acq'`, `'etl'`, `'processing'`, `'orchestration'`, `'same'`) |
| `outlets` | list[Asset] | `[]` | Assets produced by this task |
| `inlets` | list[Asset] | `[]` | Assets consumed by this task |
| `wait_for` | list[Asset] | `[]` | Assets to wait for before running: `[asset]`, `[asset.within(hours=24)]`, `[asset.consecutive(days=7)]` |

```python
from datetime import timedelta

@task.sfn(
    arn="arn:aws:states:us-east-1:123456789:stateMachine:my-sfn",
    execution_timeout=timedelta(hours=2),
    orchestration_timeout=timedelta(days=3),  # Long wait for cross-pipeline deps
    retries=2,
    retry_delay=timedelta(minutes=10),
    wait_before=60,
    trigger_rule="all_success",
    outlets=[my_asset],
)
def my_task():
    pass
```

## Task-type-specific parameters

Each task type ADDITIONALLY takes its own required / optional args on top of
the common ones — see the [Task Types](#task-types) section above (each
subsection lists them explicitly with "Required" / "Type-specific optional"
labels).

---

## Trigger Rules

> **polyris is intervention-first, not autonomous (ADR #114).** A task that exhausts
> its retries pauses for a human decision (`retry` / `mark_success` / `skip` / `fail`)
> rather than propagating failure automatically. That's a deliberate differentiator —
> most orchestrators fail and stop; polyris lets you fix it inline, in the same
> run. One consequence: a *confirmed* failure (resolved with `fail`) cancels the whole
> pipeline's `Parallel` before any downstream `trigger_rule` ever evaluates — so a rule
> whose only purpose is reacting to a confirmed failure can never fire. See ADR #117
> for the full reachable-state analysis.

polyris supports 5 trigger rules (ADR #117). Six additional rule names are
**rejected at validation time** (`polyris-validate` / `polyris-deploy`), each
with a specific suggestion for what to use instead.

> **Scope of "upstream" here — the *declared direct* deps.** A trigger rule
> looks at the tasks you connected to this task with `>>` or a function call,
> **not** transitively through the whole DAG. For `extract >> transform >>
> load`, `load`'s `trigger_rule` evaluates `transform` only — `extract` is
> reflected indirectly (if `extract` failed, `transform` never succeeded), but
> the rule never asks about `extract` directly.

| Rule | Description | Use Case |
|------|-------------|----------|
| `all_success` | All **declared direct** deps must have ended `success`. Default. | Standard ETL: extract → transform → load |
| `one_success` | At least one direct dep succeeded (fires immediately on the first one; doesn't wait for the rest) | Redundant sources: run once any input arrived |
| `all_done` | All direct deps finished (any status). Note: a *confirmed* failure cancels the run's `Parallel` before this fires — see ADR #116 for the planned exception. | Cleanup after the success path |
| `all_skipped` | All direct deps ended `skipped` | Fallback for an entirely-optional branch |
| `none_skipped` | No direct dep ended `skipped` | Only proceed if nothing upstream was intentionally skipped |

### Removed rules (ADR #117) and what to use instead

In polyris's intervention-first failure model (ADR #114), 6 additional rule names
either duplicate one of the 5 above in every reachable state, or can never fire
at all:

| Removed rule | Use instead | Why |
|------|-------------|----------|
| `one_done` | `all_done` | Identical in every reachable state |
| `none_failed` | `all_done` | Identical in every reachable state |
| `none_failed_min_one_success` | `one_success` | Identical in every reachable state |
| `all_done_min_one_success` | `one_success` | Identical in every reachable state |
| `all_failed` | *(no replacement)* | Never satisfiable — its only use case, reacting to a confirmed failure, is exactly the state `Parallel`-abort prevents it from reaching |
| `one_failed` | *(no replacement)* | Same as above |

> **Blocked-rule terminal (ADR #115).** When a rule's trigger condition never occurs
> (e.g. `all_skipped` when nothing was skipped), the task resolves **`skipped`** and
> the run stays **`success`** — this is a legitimate no-op, not an error, not
> `upstream_failed`/`aborted`. Only a rule that *requires* success (`all_success`,
> `one_success`) blocked by a **genuine, resolved** failure resolves **`upstream_failed`**.
>
> **`all_done` and a confirmed failure.** `all_done` fires only on the
> success path (every upstream ended `success` or `skipped`). A confirmed
> failure (an operator resolving a paused task with `fail`) cancels the
> run's `Parallel` before `all_done` can evaluate — see ADR #116 for the
> design.
>
> **Skip cascades for `all_success` (ADR #115).** A skipped upstream blocks
> `all_success` — but only when the skip came from a *rule*
> resolving `skipped`. A **manual** skip (an operator explicitly skipping a paused task
> to unblock it) does not cascade — a human tolerating one gap shouldn't silently
> no-op an entire downstream chain.

### Examples

```python
# all_success (default) — standard ETL
@task.sfn(arn="...:extract")
def extract(): pass

@task.sfn(arn="...:transform")
def transform(): pass

@task.sfn(arn="...:load")  # trigger_rule="all_success" implicit
def load(): pass

extract() >> transform() >> load()
# `load` runs only if `transform` ended with status `success`.
# If `extract` fails → `transform` never runs (or ends `upstream_failed`)
# → `load`'s `all_success` isn't satisfied → `load` ends `upstream_failed`.

# one_success — redundant sources, whoever wins first
@task.sfn(arn="...", trigger_rule="one_success")
def merge_results(): pass
# Fires the moment ANY direct upstream ends `success`. Does not wait for
# the others — they might still be running when `merge_results` starts.

# all_skipped — fallback branch that runs when nothing else ran
@task.sfn(arn="...", trigger_rule="all_skipped")
def fallback_path(): pass
# Runs only if every direct upstream ended `skipped` (e.g. a set of
# optional branches were all skipped by their own rules).

# all_done — cleanup that runs regardless of upstream success/skip
@task.sfn(arn="...", trigger_rule="all_done")
def cleanup(): pass
# Runs after every direct upstream has terminated (success or skipped).
# A CONFIRMED failure cancels the run's Parallel before cleanup evaluates
# — ADR #116 tracks the planned exception to react to resolved failures.
```

---

## Dependencies

### Bitshift Operators

```python
# Sequential
task_a >> task_b >> task_c

# Fan-out (one to many)
task_a >> [task_b, task_c, task_d]

# Fan-in (many to one)
[task_a, task_b, task_c] >> task_d

# Mixed
task_a >> [task_b, task_c] >> task_d
```

### Function Call Style

```python
@task.sfn(arn=...)
def extract(): pass

@task.sfn(arn=...)
def transform(): pass

@task.sfn(arn=...)
def load(): pass

# Dependencies via function calls
data = extract()
transformed = transform(data)
load(transformed)
```

### List of Dependencies

When a task depends on multiple upstream tasks:

```python
@task.sfn(arn=...)
def build_a(): pass

@task.sfn(arn=...)
def build_b(): pass

@task.sfn(arn=...)
def build_c(): pass

@task.sfn(arn=...)
def aggregate(): pass

# All three syntaxes work:
a, b, c = build_a(), build_b(), build_c()

# Option 1: List argument
aggregate([a, b, c])

# Option 2: Multiple arguments
aggregate(a, b, c)

# Option 3: Bitshift operator
[a, b, c] >> aggregate()
```

---

## Assets

### Definition

```python
from polyris import Asset

# Simple asset
processed = Asset(name="processed/acme")

# Asset with URI
processed = Asset(
    name="processed/acme",
    uri="s3://bucket/processed/acme/"
)

# Asset with group (for UI organization)
processed = Asset(
    name="processed/acme",
    group="acme"
)
```

### Producer Task (outlets)

```python
@task.sfn(arn=..., outlets=[processed])
def process_data():
    """Emits asset event when task completes."""
    pass
```

### Consumer DAG (schedule)

```python
with DAG(
    "feeds",
    schedule=[processed],  # Triggered when processed is ready
) as dag:
    ...
```

---

## Variables

### `default_args` vs `variables` — how to pick

Both are DAG-level dicts, and at first glance they look the same. They aren't.

| Question | `default_args` | `variables` |
|----------|----------------|-------------|
| What is it FOR? | Task **execution policy** — retry, timeout, role | Runtime **data** the task code / templates can read |
| Fixed at DAG-definition time? | Yes | Yes (though values can be JSONata expressions computed at run start) |
| Read by polyris internals? | Yes — merged into every task's common-parameter defaults | No — passed through to tasks as-is |
| Read by your task code? | No | Yes (via templates / xcom pull) |
| Example | `{"retries": 3, "execution_timeout": timedelta(hours=4)}` | `{"batch_size": 500, "environment": "prod"}` |

**Rule of thumb.** If it configures HOW a task runs, use `default_args`. If it's
DATA the task processes, use `variables`.

### Pipeline Variables

```python
with DAG(
    "my-pipeline",
    variables={
        "current_date": "{% $substringBefore($now(), 'T') %}",
        "environment": "prod",
    }
) as dag:
    ...
```

`variables` values may be static strings/numbers, or JSONata expressions (the
`{% ... %}` form) evaluated when the pipeline execution starts — the resolved
values are then available to every task in the run.

---

## Complete Example

```python
from polyris import DAG, task, Asset
import os

STAGE = os.environ.get("POLYRIS_STAGE", "dev")

# Assets
raw_data = Asset("raw/acme", group="acme")
processed = Asset("processed/acme", group="acme")

with DAG(
    dag_id="acme-etl",
    schedule="@daily",
    description="Acme ETL pipeline",
    tags=["production", "acme"]
) as dag:
    
    @task.sfn(
        arn=f"arn:aws:states:us-east-1:123456789:stateMachine:scrape-{STAGE}",
    )
    def scrape():
        """Scrape product data."""
        pass
    
    @task.sfn(
        arn=f"arn:aws:states:us-east-1:123456789:stateMachine:process-{STAGE}",
        outlets=[raw_data],
        retries=2
    )
    def process():
        """Process raw data."""
        pass
    
    @task.glue_job(
        job_name=f"transform-{STAGE}",
        outlets=[processed],
        trigger_rule="all_success"
    )
    def transform():
        """Transform to final format."""
        pass
    
    @task.lambda_function(function_name=f"notify-{STAGE}", trigger_rule="all_done")
    def notify():
        """Send completion notification."""
        pass
    
    # Dependencies
    s = scrape()
    p = process(s)
    t = transform(p)
    notify(t)

# Deploy
# Deploy: polyris-deploy --stage $STAGE
```

---

## CLI Usage

Run these from the pipeline directory:

```bash
# Validate pipeline
polyris-validate

# Validate with details
polyris-validate -v

# Generate Step Functions JSON
polyris-output --json

# Generate Mermaid diagram
polyris-output --mermaid

# Show DAG as ASCII graph
polyris-output --graph

# Generate asset registry JSON
polyris-output --assets

# Deploy
polyris-deploy --stage dev
```
