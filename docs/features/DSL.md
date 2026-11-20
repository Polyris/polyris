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
monitor logic. Before you write
`@task.sfn(arn="arn:aws:states:us-east-1:123:stateMachine:my-workflow")`, that
state machine must already exist, created by your own provisioning tool (SAM,
CDK, Terraform, hand-created). Same for Lambda / Glue / ECS / Athena / EMR /
Batch — polyris expects them there and calls them by AWS identifier.

The one thing polyris deploys is your pipeline itself — a set of orchestration
state machines that glue your existing resources into a DAG. For the full
wrapper-vs-workload split, see
[INFRASTRUCTURE.md § The one distinction that matters](../deployment/INFRASTRUCTURE.md#the-one-distinction-that-matters-wrappers-vs-workloads).

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
    group="orders",
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
| `group` | str | No | Group the console's sidebar lists the pipeline under, e.g. `"orders"`; compared without case. Without it, the pipeline is listed under *Ungrouped*, or in a flat list when no pipeline sets a group. A change shows after the next deploy or run |
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
        "retries": 0,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),       # One attempt of the work: 4h at most
        "orchestration_timeout": timedelta(hours=12),  # All waiting: 12h at most
    }
) as dag:
    # All tasks inherit these defaults unless overridden
    @task.sfn(arn=..., retries=3)  # Override: retry this task up to 3 times
    def retried_task(): pass
```

### Alerts Configuration

> **Removed.** The `alerts=` argument has been removed — passing
> `alerts={...}` now raises a `TypeError`. Remove it from your DAGs. Alert
> delivery moved out of the DSL:
>
> - **Browser notifications** (in-app) are automatic and free — no setup.
> - Alert config is not part of the DSL — there is no `alerts=` argument.
>
> `DAG` has no `alerts=` argument — passing `alerts={...}` now raises
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

# Single asset: one run per date. A producer that materializes asset_a
# twice for the same date starts this consumer once, for that date.
DAG(schedule=asset_a)

# Multiple assets, ALL required (AND): one run per date, once every asset
# has a fact that serves it (see ASSETS.md, Granularity).
DAG(schedule=[asset_a & asset_b])

# Multiple assets, ANY triggers (OR): one run per materialization of either.
DAG(schedule=[asset_a | asset_b])

# Single asset, but one run per materialization: an explicit AssetAny of one
# item. Use it for a producer that runs more than once a day by design,
# where every run should recompute the consumer.
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
    task_definition="my-task",
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
| `task_id` | str | (inferred from function name) | Override the auto-inferred task id, at most 128 bytes in UTF-8. The task's Step Functions execution is named `{task_id}-{date}-{run suffix}`; an id over 57 characters appears there as a prefix plus a 6-character hash |
| `execution_timeout` | timedelta | none | The limit of one attempt of the work. A timeout is a failed attempt: the task's retries follow, then the decision on a failed task. Unset, the work has no limit |
| `orchestration_timeout` | timedelta | none | A cap on all of the task's waiting, until it is ready. It never falls back to `execution_timeout`. Unset, a task waits for the tasks of its run as long as they take, and for its `wait_for` assets 7 days |
| `retries` | int | 0 | Number of retry attempts. Maximum: `9` (values above 9 raise `ValueError` at DAG-definition time) |
| `retry_delay` | timedelta | 5 minutes | Delay between retries (base delay when backoff is on) |
| `retry_exponential_backoff` | bool | False | Double the wait each retry: `min(retry_delay·2^n, max_retry_delay)` |
| `max_retry_delay` | timedelta | none (3600s cap) | Ceiling for exponential backoff |
| `retry_jitter` | bool | False | Randomise each wait into `[base/2, base)` (avoids retry stampede) |
| `wait_before` | int (seconds) or timedelta | 0 | Wait this long before executing (rate limiting) |
| `trigger_rule` | str | `"all_success"` | Condition on **declared direct upstream** deps — see [Trigger Rules](#trigger-rules) below |
| `role` | str | `"same"` | Cross-account role: a key of the `roles` dict in `config.py` (e.g. `'analytics'`, see [CONFIGURATION.md](../reference/CONFIGURATION.md)), a role ARN, or `'same'` for the platform's own role |
| `outlets` | list[Asset] | `[]` | Assets produced by this task: at most 98, each once (the decorator refuses an asset named twice) |
| `inlets` | list[Asset] | `[]` | Assets consumed by this task |
| `wait_for` | list[Asset] | `[]` | Assets to wait for before running, each for the run's date ([ASSETS.md § Granularity](ASSETS.md#granularity)): `[asset]`, `[asset.within(hours=24)]`, `[asset.consecutive(days=7)]`. The wait lasts 7 days from when the task's own dependencies are met; then the task fails. `polyris-deploy` warns when `orchestration_timeout` is shorter |

```python
from datetime import timedelta

@task.sfn(
    arn="arn:aws:states:us-east-1:123456789:stateMachine:my-sfn",
    execution_timeout=timedelta(hours=2),     # One attempt of the work: 2h at most
    orchestration_timeout=timedelta(days=8),  # All waiting: 8 days at most
    retries=2,
    retry_delay=timedelta(minutes=10),
    wait_before=60,
    trigger_rule="all_success",
    outlets=[my_asset],
)
def my_task():
    pass
```

### Pipeline limits

`polyris-validate` and `polyris-deploy` refuse a pipeline beyond any of these limits, naming the limit and the size they measured:

| Limit | Value | Why |
|---|---|---|
| Tasks per pipeline | 2,000 | The declared maximum the platform is tested at |
| Outlets per task | 98 | A task's success is recorded in one DynamoDB transaction of at most 100 items |
| A task's spec | 256 KB | A task starts with its spec as its input, which Step Functions caps at 256 KB |
| The specs together | 16 MB | A run's first step reads every spec of the pipeline at once |
| `task_id` | 128 bytes | Operator requests name a task in full |
| The definition | 1 MB | Step Functions' limit, measured as `polyris-deploy` sends it |

`polyris-output --json` shows each task's spec and the definition.

## Task-type-specific parameters

Each task type ADDITIONALLY takes its own required / optional args on top of
the common ones — see the [Task Types](#task-types) section above (each
subsection lists them explicitly with "Required" / "Type-specific optional"
labels).

---

## Trigger Rules

A trigger rule decides when a task runs, from the outcomes of its **declared direct** upstreams. `all_success` is the default.

> **Scope of "upstream" here — the *declared direct* deps.** A trigger rule
> looks at the tasks you connected to this task with `>>` or a function call,
> **not** transitively through the whole DAG. For `extract >> transform >>
> load`, `load`'s `trigger_rule` evaluates `transform` only — `extract` is
> reflected indirectly (if `extract` failed, `transform` never succeeded), but
> the rule never asks about `extract` directly.

| Rule | Runs its task when | Example |
|------|--------------------|---------|
| `all_success` (default) | Every upstream succeeded, or was skipped by an operator or by the run | `extract >> transform >> load` |
| `one_success` | One upstream succeeded. The task starts at the first success and does not wait for the others | Two vendors deliver the same prices: process whichever arrives first |
| `all_done` | Every upstream is final, whatever the outcome | Stop the EMR cluster after `load`, also when `load` failed |
| `all_skipped` | Every upstream was skipped | `load_archive` runs when the run skipped `scrape_live` (`skip_tasks`) |
| `none_skipped` | Every upstream is final and none was skipped. Failures pass | A data-quality report on every source, failures included |
| `one_failed` | One upstream failed finally. The task starts at the first final failure | Delete the half-written S3 partition that a failed `load` left |
| `all_failed` | Every upstream failed finally | Both price feeds failed: load yesterday's prices |
| `none_failed` | Every upstream is final and none failed. Skips pass, even when every upstream was skipped | Write the date's `_SUCCESS` marker when nothing failed |
| `none_failed_min_one_success` | Every upstream is final, none failed and at least one succeeded. Skips pass | `report` after `core` and an optional `enrich`, which a rule may skip |

### What a rule reads

- **Final outcomes.** An upstream is final once it succeeded, was skipped, ended `upstream_failed`, failed finally or was stopped. A task that exhausts its retries waits for a human decision first (ADR #114): retry, mark successful, skip or fail. Until that decision, or the decision timeout (5 hours by default), it is pending for every rule, so `one_failed` runs after the decision, not at the first error. The console bell shows the failure at once.
- **Final failures** are `failed`, `upstream_failed` and `stopped`. A Stop is final: the stopped task's dependants are decided as after a Fail. Until a dependant is decided, a Restart or Mark Successful of the stopped task still counts for it.
- **When a rule decides.** A rule runs its task as soon as its condition holds: `one_success` at the first success, `one_failed` at the first final failure, every other rule once all its upstreams are final. It skips the task, or ends it `upstream_failed`, only once every upstream is final.
- **`skipped` or `upstream_failed`.** A rule that needs success or no failure (`all_success`, `one_success`, `none_failed`, `none_failed_min_one_success`) ends its task `upstream_failed` when a final failure blocks it. Any other rule that can no longer be met ends its task `skipped`: the condition never occurred, which is not an error (ADR #115).

### Skips and who made them

Every skip records who made it, in `skip_origin`:

| Origin | Who skips | Under `all_success` |
|--------|-----------|---------------------|
| `manual` | An operator, from the console | Passes, as if the task had succeeded |
| `run` | The run, for a task named in its `skip_tasks` | Passes, as if the task had succeeded |
| `rule` | The engine, because the task's own rule can no longer be met | Cascades: the dependant is skipped too |

Only `all_success` reads the origin. Every other rule reads a skip the same: `one_success` and `none_failed_min_one_success` never count it as a success, `none_skipped` skips its task, and `all_skipped` counts it.

> **Coming from Airflow.** In Airflow every skip cascades under `all_success`, and a join after a branch needs `none_failed_min_one_success`. In polyris only a rule's skip cascades: a task that an operator or the run skipped lets its `all_success` dependants run. To skip a chain by hand, skip each task in it.

### Names that are rejected

`polyris-validate` and `polyris-deploy` reject any other name. Four names that other orchestrators use get a hint:

| Name | Use instead |
|------|-------------|
| `one_done` | `all_done` to wait for every upstream; `one_success` or `one_failed` to react to the first outcome of one kind |
| `all_done_min_one_success` | `one_success`, which starts at the first success instead of waiting for the rest |
| `always` | No rule: remove the dependency, since a task without upstreams starts with its run |
| `all_done_setup_success` | `all_done` for a cleanup task |

A task whose rule the platform does not know, from a pipeline deployed with a newer SDK, fails with the cause when its run reaches it.

### A failure and the run

A final failure does not end the run at once: its dependants' rules decide, and independent tasks run to completion. A run with a final failure fails, even when a cleanup (`all_done`) or a compensation (`one_failed`) task succeeded after it. To let a run succeed despite a failed task, skip the task or mark it successful before its failure is final.

### Examples

```python
from polyris import DAG, task

with DAG("orders", schedule="@daily") as dag:

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:load-orders")
    def load():
        """Default all_success: runs when every upstream succeeded."""

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:stop-cluster",
              trigger_rule="all_done")
    def stop_cluster():
        """Cleanup: runs once load is final, success or failure."""

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:drop-partition",
              trigger_rule="one_failed")
    def drop_partition():
        """Compensation: runs after load failed finally, to remove what it half wrote."""

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:mark-done",
              trigger_rule="none_failed")
    def mark_done():
        """Writes the _SUCCESS marker when nothing failed; a skipped load still passes."""

    loaded = load()
    loaded >> stop_cluster()
    loaded >> drop_partition()
    loaded >> mark_done()
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

Passing a task's result to another task only makes it a dependency: no data moves through
the argument, and the function body does not run in AWS. A task's output reaches the next
task through xcom ([DATA_PASSING.md](DATA_PASSING.md)). Prefer `>>`: it says what it does.

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

# Asset with a group
processed = Asset(
    name="processed/acme",
    group="acme"
)

# Asset with a weekly partition (default: daily)
prices = Asset(name="pricing/prices", granularity="weekly")
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
        "run_id": "{% $states.context.Execution.Name %}",
        "environment": "prod",
    }
) as dag:
    ...
```

`variables` values may be static strings/numbers, or JSONata expressions (the
`{% ... %}` form) evaluated when the pipeline execution starts — the resolved
values are then available to every task in the run.

### Date variables

A run has one date, the same for every task (ADR #133). It is the date the run was started for: the one you pick in the console, or, for a run an asset started, the date of that asset's fact. A run its schedule started gets the UTC day the schedule fired it for, even if it started after midnight; a run started any other way gets the UTC day it started.

Every task's `variables` also carry the run's date in several forms; a variable of yours with the same name wins. For a run dated 2025-07-25:

| Variable | Value |
|----------|-------|
| `current_date`, `date_iso` | `"2025-07-25"` |
| `date_compact` | `"20250725"` |
| `date_slash` | `"2025/07/25"` |
| `date_underscore` | `"2025_07_25"` |
| `year`, `month`, `day` | `"2025"`, `"07"`, `"25"` |
| `day_of_week` | `"friday"` |
| `previous_date`, `next_date` | `"2025-07-24"`, `"2025-07-26"` |
| `minus_7_days`, `minus_14_days`, `minus_30_days` | `"2025-07-18"`, `"2025-07-11"`, `"2025-06-25"` |
| `minus_7_days_compact`, `minus_30_days_compact` | `"20250718"`, `"20250625"` |

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

## JSONata expressions (reference)

The generated ASL uses **JSONata** as its query language. When writing
custom `@task.sfn` bodies, `variables=` values, or reading intermediate
state, these five patterns cover most cases:

**Get today's date (UTC):**
```
{% $substringBefore($now(), 'T') %}
```

**Conditional / default:**
```
{% $exists($states.input.foo) ? $states.input.foo : 'default' %}
```

**String concatenation:**
```
{% 'prefix-' & $states.input.name & '-suffix' %}
```

**Filter an array:**
```
{% $filter($states.input.items, function($v) { $v.status = 'active' }) %}
```

**Merge two objects:**
```
{% $merge([$states.input, {'new_field': 'value'}]) %}
```

Full JSONata reference: [jsonata.org/docs](https://docs.jsonata.org/).

---

## CLI Usage

Run these from the pipeline directory:

```bash
# Validate pipeline
polyris-validate

# Validate with details
polyris-validate -v

# The Step Functions definition and its task specs
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
