# Python DSL reference

You describe a pipeline in a `dag.py` file: a `DAG(...)`, its tasks as `@task.<service>` functions, and the dependencies between them with `>>`. `polyris-deploy` turns the file into a Step Functions state machine and a schedule in your AWS account. Terms such as *run*, *run's date*, *fact* and *decision* are defined in [GLOSSARY.md](../../GLOSSARY.md).

> Passing data between tasks is a separate topic: see [DATA_PASSING.md](DATA_PASSING.md) for `xcom`, and how each task type reads its upstream's output.

## Contents

- [How polyris relates to AWS](#how-polyris-relates-to-aws): decorators orchestrate existing resources
- [DAG definition](#dag-definition)
- [Schedule options](#schedule-options)
- [Task types](#task-types): `sfn`, `lambda_function`, `glue_job`, `ecs_task`, `athena_query`, `emr_step`, `batch_job`
- [Common parameters](#common-parameters-apply-to-every-task-type): retry, timeout, trigger rule, role, assets
- [Trigger rules](#trigger-rules)
- [Dependencies](#dependencies): `>>`, `chain()`, `cross_downstream()`, task groups
- [Direct steps](#direct-steps): service calls without a task
- [Assets](#assets)
- [Variables](#variables)
- [Complete example](#complete-example)

## How polyris relates to AWS

**Every `@task.<service>` decorator orchestrates an AWS resource that already exists.** polyris does not create the state machine, function, job, cluster or database: it starts it, retries it and records its outcome. Before you write `@task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:my-workflow")`, that state machine must exist, created by your own tool (SAM, CDK, Terraform, or by hand). The same holds for Lambda, Glue, ECS, Athena, EMR and Batch: polyris calls them by their AWS identifier.

The one thing polyris deploys is your pipeline itself: a state machine that runs your existing resources as a DAG. For the split between the platform and your workloads, see [INFRASTRUCTURE.md § The one distinction that matters](../deployment/INFRASTRUCTURE.md#the-one-distinction-that-matters-the-platform-vs-your-workloads).

### Namespace

`namespace` is the prefix you gave `polyris-up`. It names every resource of the platform and your pipelines' stacks, so two installs in one account don't collide. It has nothing to do with `dag_id` or your ARNs: your `@task.sfn(arn=...)` values point at whatever ARNs you have. A `config.py` can set another namespace for your pipelines ([CONFIGURATION.md](../reference/CONFIGURATION.md)).

---

## DAG definition

```python
from polyris import DAG

with DAG(
    dag_id="my-pipeline",
    schedule="@daily",
    description="My data pipeline",
    group="orders",
    tags=["production", "etl"],
    variables={"batch_size": "500"},
) as dag:
    ...
```

### DAG parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `dag_id` | str | Yes | Unique pipeline identifier |
| `schedule` | str/Asset/list | No | Schedule: cron, preset, or assets ([Schedule options](#schedule-options)) |
| `description` | str | No | Human-readable description |
| `tags` | list | No | Tags for organization |
| `group` | str | No | Group the console's sidebar lists the pipeline under, e.g. `"orders"`; compared without case. Without it, the pipeline is listed under *Ungrouped*, or in a flat list when no pipeline sets a group. A change shows after the next deploy or run |
| `variables` | dict | No | Values passed to the tasks ([Variables](#variables)) |
| `default_args` | dict | No | Default common parameters for all tasks (see below) |
| `is_paused_upon_creation` | bool | No | `True` deploys the schedule disabled, and every later `polyris-deploy` disables it again. Unset or `False`, every deploy enables it |

### Default args

Apply shared parameters to all tasks in a DAG:

```python
from datetime import timedelta

from polyris import DAG, task

with DAG(
    "my-pipeline",
    schedule="@daily",
    default_args={
        "retries": 0,
        "retry_delay": timedelta(minutes=10),
        "execution_timeout": timedelta(hours=4),       # One attempt of the work: 4h at most
        "orchestration_timeout": timedelta(hours=12),  # All waiting: 12h at most
    },
) as dag:
    # Every task inherits these defaults unless it overrides them
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:my-sfn", retries=3)
    def retried_task():
        pass
```

## Schedule options

### Time-based

```python
# Presets: each fires at the start of its period, in UTC
DAG(schedule="@hourly")     # cron(0 * * * ? *)    every hour, on the hour
DAG(schedule="@daily")      # cron(0 0 * * ? *)    00:00
DAG(schedule="@weekly")     # cron(0 0 ? * MON *)  Monday 00:00
DAG(schedule="@monthly")    # cron(0 0 1 * ? *)    the 1st, 00:00
DAG(schedule="@yearly")     # cron(0 0 1 1 ? *)    1 January, 00:00

# Custom cron: EventBridge's six fields (minutes hours day-of-month month day-of-week year)
DAG(schedule="cron(0 8 * * ? *)")         # 08:00 UTC daily
DAG(schedule="cron(0 9 ? * MON-FRI *)")   # 09:00 UTC on weekdays

# Rate expressions
DAG(schedule="rate(6 hours)")
```

A run that the schedule starts is dated by the UTC day the schedule fired it for, whatever the preset: every `@hourly` run of 25 July is dated `2025-07-25`. The `@daily` run at 00:00 on 25 July is the run of a day that has just begun. To process the day before, take the run's date minus one day in the task, or schedule a later `cron(...)`. A `rate(...)` schedule fires at intervals counted from when `polyris-deploy` created the schedule; to run at a fixed time, use `cron(...)` or a preset. A bare five-field cron such as `"0 9 * * MON-FRI"` is refused: write `cron(0 9 ? * MON-FRI *)`.

### Asset-based (cross-pipeline triggers)

```python
from polyris import DAG, Asset, AssetAny

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

### Manual only

```python
DAG(schedule=None)  # No automatic trigger: start runs with Run in the console
```

---

## Task types

Seven decorators are shipped. Each one orchestrates a specific AWS service. All of them accept the shared [common parameters](#common-parameters-apply-to-every-task-type) (retry, timeout, trigger rule, role, assets) on top of their type-specific arguments.

### `@task.sfn`: AWS Step Functions

Executes a nested Step Function state machine.

**Required:**
- `arn` (str): full ARN of the state machine to invoke.

**Type-specific (optional):** none.

```python
@task.sfn(
    arn="arn:aws:states:us-east-1:123456789012:stateMachine:my-sfn",
    # Any common parameter is welcome here too:
    retries=2,
    trigger_rule="all_success",
)
def my_task():
    pass
```

### `@task.lambda_function`: AWS Lambda

Invokes an existing Lambda function directly (not via a nested Step Function).

**Required (one of):**
- `function_name` (str): function name in the current region.
- `arn` (str): full function ARN (use this for cross-region / cross-account).

**Type-specific (optional):**
- `payload` (dict): JSON input passed to the Lambda `event`.

```python
@task.lambda_function(
    function_name="my-function",
    payload={"key": "value"},
)
def process_data():
    pass
```

### `@task.glue_job`: AWS Glue

Starts an existing Glue job run.

**Required:**
- `job_name` (str): Glue job name.

**Type-specific (optional):**
- `glue_arguments` (dict[str, str]): `--key value` overrides for the job.
- `worker_type` + `number_of_workers`: must be set together (`G.1X` / `G.2X` / etc). For PySpark (glueetl) jobs.
- `max_capacity` (float): DPU allocation for Python Shell jobs. Supports fractional values (e.g. `0.0625` for 1/16 DPU). Mutually exclusive with `worker_type`/`number_of_workers`.
- `allocated_capacity` (int): integer DPU model (deprecated by AWS). Use `max_capacity` instead: it supports the same whole-DPU values and also fractional values.
- `command_name` (str): `"pythonshell"` or `"glueetl"`. Enables cross-type validation (e.g. rejects `worker_type` on a Python Shell job).

```python
# PySpark job
@task.glue_job(
    job_name="my-etl-job",
    glue_arguments={"--date": "2024-01-01"},
    worker_type="G.1X",
    number_of_workers=2,
    command_name="glueetl",  # optional: enables cross-type validation
)
def etl_job():
    pass

# Python Shell with 1/16 DPU
@task.glue_job(job_name="light-job", max_capacity=0.0625, command_name="pythonshell")
def light_job():
    pass
```

### `@task.ecs_task`: Amazon ECS (Fargate or EC2)

Runs an existing ECS task definition.

**Required:**
- `cluster` (str): ECS cluster name.
- `task_definition` (str): task definition family name, without a `:N` revision: each run uses the latest active revision.

**Type-specific (optional):**
- `launch_type` (str, default `"FARGATE"`): `FARGATE` or `EC2`.
- `subnets` (list[str]): **required if `launch_type="FARGATE"`** (Fargate runs in an ENI).
- `security_groups` (list[str]).
- `assign_public_ip` (str, default `"DISABLED"`): `"ENABLED"` or `"DISABLED"`.
- `container_overrides` (dict): passed through to `RunTask`.

```python
@task.ecs_task(
    cluster="my-cluster",
    task_definition="my-task",
    launch_type="FARGATE",
    subnets=["subnet-0a1b2c3d"],
    security_groups=["sg-0a1b2c3d"],
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

`NetworkConfiguration` is sent only when `subnets` are provided; EC2 launch type with `bridge` / `host` networking can omit them.

### `@task.athena_query`: Amazon Athena

Runs a SQL query against an existing Athena database.

**Required:**
- `query_string` (str): SQL to execute.
- `database` (str): Athena database.

**Type-specific (optional):**
- `output_location` (str): S3 URL for results. Omitting it leaves the query's output location to the workgroup.
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

### `@task.emr_step`: Amazon EMR

Adds a single step to an existing EMR cluster (`elasticmapreduce:addStep`).

**Required:**
- `emr_cluster_id` (str): existing EMR cluster ID (`j-XXXX...`).
- `emr_step` (dict): AWS `StepConfig` dict. Must contain `HadoopJarStep.Jar`.

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

### `@task.batch_job`: AWS Batch

Submits an existing Batch job definition to a job queue.

**Required:**
- `job_definition` (str): Batch job definition name, without a `:N` revision: each run uses the latest active revision.
- `job_queue` (str): Batch job queue (`name` or `arn`).

**Type-specific (optional):**
- `batch_parameters` (dict[str, str]): substitution parameters passed to `SubmitJob`.

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

Every `@task.<service>` accepts these. They control retry, timeout and trigger policy, and are the same for every task type.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `task_id` | str | (inferred from function name) | Override the auto-inferred task id, at most 128 bytes in UTF-8. The task's Step Functions execution is named `{task_id}-{date}-{run suffix}`, cut to 63 characters, followed by a 16-character hash; a Restart gets a new name |
| `execution_timeout` | timedelta | none | The limit of one attempt of the work. A timeout is a failed attempt: the task's retries follow, then the [decision](../../GLOSSARY.md#decision) on a failed task. A retry after a timeout waits at least a minute, while Step Functions stops the timed-out work; a Lambda invocation runs on until its own timeout. Unset, the work has no limit |
| `orchestration_timeout` | timedelta | none | A cap on all of the task's waiting, until it is ready. It never falls back to `execution_timeout`. Unset, a task waits for the tasks of its run as long as they take, and for its `wait_for` assets 7 days |
| `retries` | int | 0 | Number of retry attempts. Maximum: `9` (values above 9 raise `ValueError` at DAG-definition time) |
| `retry_delay` | timedelta | 5 minutes | Delay between retries (base delay when backoff is on) |
| `retry_exponential_backoff` | bool | False | Double the wait each retry: `min(retry_delay·2^n, max_retry_delay)` |
| `max_retry_delay` | timedelta | none (3600s cap) | Ceiling for exponential backoff |
| `retry_jitter` | bool | False | Randomise each wait into `[base/2, base)` (avoids retry stampede) |
| `wait_before` | int (seconds) or timedelta | 0 | Wait this long before executing (rate limiting). The wait ends at the platform's next check after it, up to 5 minutes later |
| `trigger_rule` | str | `"all_success"` | Condition on **declared direct upstream** deps: see [Trigger rules](#trigger-rules) below |
| `role` | str | `"same"` | Cross-account role: a key of the `roles` dict in `config.py` (e.g. `'analytics'`, see [CONFIGURATION.md](../reference/CONFIGURATION.md)), a role ARN, or `'same'` for the platform's own role |
| `outlets` | list[Asset] | `[]` | Assets produced by this task: at most 97, each once (the decorator refuses an asset named twice) |
| `inlets` | list[Asset] | `[]` | Assets consumed by this task (documentation only) |
| `wait_for` | list[Asset] | `[]` | Assets to wait for before running, each for the [run's date](../../GLOSSARY.md#a-runs-date) ([ASSETS.md § Granularity](ASSETS.md#granularity)): `[asset]`, `[asset.within(hours=24)]`, `[asset.consecutive(days=7)]`. The wait lasts 7 days from when the task's own dependencies are met; then the task fails. `polyris-deploy` warns when `orchestration_timeout` is shorter |

```python
from datetime import timedelta

from polyris import Asset, task

orders = Asset("clean/orders")

@task.sfn(
    arn="arn:aws:states:us-east-1:123456789012:stateMachine:my-sfn",
    execution_timeout=timedelta(hours=2),     # One attempt of the work: 2h at most
    orchestration_timeout=timedelta(days=8),  # All waiting: 8 days at most
    retries=2,
    retry_delay=timedelta(minutes=10),
    wait_before=60,
    trigger_rule="all_success",
    outlets=[orders],
)
def my_task():
    pass
```

### Pipeline limits

`polyris-validate` and `polyris-deploy` refuse a pipeline beyond any of these limits, naming the limit and the size they measured:

| Limit | Value | Why |
|---|---|---|
| Tasks per pipeline | 2,000 | The declared maximum the platform is tested at |
| Outlets per task | 97 | A task's success is recorded in one DynamoDB transaction of at most 100 items |
| A task's spec | 256 KB | A task starts with its spec as its input, which Step Functions caps at 256 KB |
| A task's spec with its upstreams' statuses | 256 KB | The input also carries each upstream's status, about 50 bytes and its name per upstream, so a wide fan-in counts |
| A task's upstreams × its attempts | 22,500 history events | Each attempt reads every upstream's output, about 7 events each, and Step Functions caps an execution at 25,000 events: with `retries=3`, about 800 upstreams |
| An ECS task's container overrides | 8,192 characters | ECS's limit, measured with the environment the platform adds to each container override, the upstreams' statuses included |
| A Batch task's `SubmitJob` request | 30 KiB | Batch's limit, measured with the environment the platform adds, the upstreams' statuses included |
| The specs together | 16 MB | A run's first step reads every spec of the pipeline at once |
| `task_id` | 128 bytes, and no leading `_` | Operator requests name a task in full; `_` starts the platform's own rows |
| The definition | 1 MB | Step Functions' limit, measured as `polyris-deploy` sends it |

`polyris-output --json` shows each task's spec and the definition.

---

## Trigger rules

A trigger rule decides when a task runs, from the outcomes of its **declared direct** upstreams. `all_success` is the default.

> **Scope of "upstream" here: the *declared direct* deps.** A trigger rule looks at the tasks you connected to this task with `>>` or a function call, **not** transitively through the whole DAG. For `extract >> transform >> load`, `load`'s `trigger_rule` evaluates `transform` only: `extract` is reflected indirectly (if `extract` failed, `transform` never succeeded), but the rule never asks about `extract` directly.

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

- **Final outcomes.** An upstream is final once it succeeded, was skipped, ended `upstream_failed`, failed finally or was stopped. A task that exhausts its retries waits for a human [decision](../../GLOSSARY.md#decision) first: retry, mark successful, skip or fail. Until that decision, or until it fails after 5 hours without one, it is pending for every rule, so `one_failed` runs after the decision, not at the first error. The console bell shows the failure at once.
- **Final failures** are `failed`, `upstream_failed` and `stopped`. A Stop is final: the stopped task's dependants are decided as after a Fail. Until a dependant is decided, a Restart or Mark Successful of the stopped task still counts for it.
- **When a rule decides.** A rule runs its task as soon as its condition holds: `one_success` at the first success, `one_failed` at the first final failure, every other rule once all its upstreams are final. It skips the task, or ends it `upstream_failed`, only once every upstream is final.
- **What the task then reads.** A rule reads statuses; the task it runs reads outputs. It gets an upstream's output only when that upstream succeeded or an operator or the run skipped it, and otherwise its status alone, so it never reads an older run's output of the same date. Through `xcom`, the output of an upstream that failed finally raises `XComUpstreamFailedError`, and of one that has not succeeded yet in this run (beside a `one_success` task) `XComMissingError` ([DATA_PASSING.md](DATA_PASSING.md#reading-upstream-xcomget)).
- **`skipped` or `upstream_failed`.** A rule that needs success or no failure (`all_success`, `one_success`, `none_failed`, `none_failed_min_one_success`) ends its task `upstream_failed` when a final failure blocks it. Any other rule that can no longer be met ends its task `skipped`: the condition never occurred, which is not an error.

### Skips and who made them

Every skip records who made it, in `skip_origin`:

| Origin | Who skips | Under `all_success` |
|--------|-----------|---------------------|
| `manual` | An operator, from the console | Passes, as if the task had succeeded |
| `run` | The run, for a task named in its `skip_tasks` | Passes, as if the task had succeeded |
| `rule` | polyris, because the task's own rule can no longer be met | Cascades: the dependant is skipped too |

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

### Bitshift operators

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

### Function call style

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

A task that depends on several takes them all, or a list: `aggregate(a, b, c)`, `aggregate([a, b, c])` and `[a, b, c] >> aggregate()` do the same.

Passing a task's result to another task only makes it a dependency: no data moves through the argument, and the function body does not run in AWS. A task's output reaches the next task through xcom ([DATA_PASSING.md](DATA_PASSING.md)). Prefer `>>`: it says what it does.

### `chain()` and `cross_downstream()`

`chain(a, b, c)` is `a >> b >> c`. A list in the chain fans out and back in; two lists next to each other connect pairwise and must have the same length. `cross_downstream(ups, downs)` makes every task of `downs` depend on every task of `ups`.

```python
from polyris import DAG, task, chain, cross_downstream

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:"

with DAG("reports", schedule="@daily") as dag:

    @task.sfn(arn=ARN + "extract")
    def extract(): pass

    @task.sfn(arn=ARN + "clean-orders")
    def clean_orders(): pass

    @task.sfn(arn=ARN + "clean-refunds")
    def clean_refunds(): pass

    @task.sfn(arn=ARN + "feed-shop")
    def feed_shop(): pass

    @task.sfn(arn=ARN + "feed-partner")
    def feed_partner(): pass

    cleaned = [clean_orders(), clean_refunds()]
    chain(extract(), cleaned)                              # extract >> both clean tasks
    cross_downstream(cleaned, [feed_shop(), feed_partner()])  # each feed waits for both
```

### Task groups

A task group puts a prefix on the ids of the tasks defined in it (`extract.orders`) and lets you connect the group as one. `group >> task` makes the task depend on the group's last tasks; `task >> group` makes the group's first tasks depend on it.

```python
from polyris import DAG, TaskGroup, task

ARN = "arn:aws:states:us-east-1:123456789012:stateMachine:"

with DAG("orders", schedule="@daily") as dag:

    with TaskGroup("extract") as extract:

        @task.sfn(arn=ARN + "extract-orders")
        def orders(): pass                  # task id: extract.orders

        @task.sfn(arn=ARN + "extract-customers")
        def customers(): pass               # task id: extract.customers

    @task.sfn(arn=ARN + "load-orders")
    def load(): pass

    extract >> load()
```

`@task_group()` on a function does the same, with the function's name as the group id: `extract() >> load()`. `TaskGroup("extract", prefix_group_id=False)` keeps the tasks' own ids.

---

## Direct steps

A direct step is a single AWS service call that runs inside the pipeline's own state machine, without a task: publish to SNS, send to SQS, write to S3. Every direct step starts with the run, beside its tasks, and has no dependencies: a task cannot depend on a step, and a step cannot depend on a task. A step that fails fails the run, once its other steps and tasks have ended, and the bell shows why. The console does not show steps in the graph.

```python
from polyris import DAG, SNSTask, S3Task, task

with DAG("orders-daily", schedule="@daily") as dag:

    @task.lambda_function(function_name="load-orders")
    def load():
        pass

    SNSTask(
        step_id="announce_start",
        topic_arn="arn:aws:sns:us-east-1:123456789012:pipeline-events",
        message="{% 'orders-daily started for ' & $states.input.current_date %}",
    )

    S3Task(
        step_id="write_marker",
        operation="put_object",
        bucket="acme-reports",
        key="{% 'markers/' & $states.input.current_date & '/started' %}",
        body="{% $string({'date': $states.input.current_date}) %}",
    )

    load()
```

A string value is either text or, whole, a JSONata expression in `{% ... %}`. An expression reads the run's date as `$states.input.current_date` and a DAG variable as `$states.input.variables.<name>`.

| Class | Calls | Required arguments |
|-------|-------|--------------------|
| `SNSTask` | `sns:Publish` | `topic_arn`, `message` (optional `subject`, `message_attributes`) |
| `SQSTask` | `sqs:SendMessage` | `queue_url` (optional `message_body`, `delay_seconds`, `message_group_id`) |
| `S3Task` | `get_object`, `put_object`, `copy_object`, `delete_object` | `bucket`, `key`; `body` for `put_object`, `copy_source` for `copy_object` |
| `DynamoDBTask` | `get_item`, `put_item`, `update_item`, `delete_item`, `query`, `scan` | `table_name`, `operation`, and `key`, `item`, `update_expression` or `key_condition` as the operation needs |
| `EventBridgeTask` | `events:PutEvents` | `source`, `detail_type` (optional `event_bus`, `detail`) |
| `HttpTask` | an HTTPS endpoint | `url`, and `connection_arn`: the EventBridge connection that holds the endpoint's credentials, which Step Functions requires (optional `method`, `headers`, `body`) |
| `BedrockTask` | `bedrock:InvokeModel` | `model_id`, `body` |

Give each step a `step_id` unique in the pipeline. `Wait`, `Pass` and `Succeed` exist too, but since nothing follows a step, they change nothing in a run: for a delay before a task, use `wait_before`. `LambdaTask`, `GlueTask`, `AthenaTask` and `ECSTask` run as tasks, not steps; use the `@task` decorators of the same services instead.

> **Note:** The pipeline's state machine runs as the platform's role `<namespace>-<stage>-polyris-orchestration-role`, which may make each of these calls on any resource of the account ([IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md)). A resource in another account, or one whose policy or KMS key refuses the role, still answers `AccessDenied`.

---

## Assets

A task declares the data it produces with `outlets`; another pipeline starts on it with `schedule=`, or one of its tasks waits for it with `wait_for`:

```python
from polyris import DAG, Asset, task

orders = Asset("clean/orders")

with DAG("orders-etl", schedule="@daily") as producer:

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:clean-orders", outlets=[orders])
    def clean():
        pass

with DAG("orders-report", schedule=orders) as consumer:

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:build-report")
    def build_report():
        pass
```

Granularity, AND/OR schedules, `wait_for` freshness and schemas: [ASSETS.md](ASSETS.md).

---

## Variables

### `default_args` or `variables`

Both are DAG-level dicts. They are not the same:

| Question | `default_args` | `variables` |
|----------|----------------|-------------|
| What is it for? | Task **execution policy**: retry, timeout, role | Runtime **data** the task code can read |
| Fixed at DAG-definition time? | Yes | Yes (though values can be JSONata expressions computed at run start) |
| Read by polyris? | Yes: merged into every task's common-parameter defaults | No: passed through to tasks as-is |
| Read by your task code? | No | Yes: a Lambda or `@task.sfn` task gets each variable as a key of its input (`event["batch_size"]`) |
| Example | `{"retries": 3, "execution_timeout": timedelta(hours=4)}` | `{"batch_size": 500, "environment": "prod"}` |

**Rule of thumb.** If it configures how a task runs, use `default_args`. If it is data the task processes, use `variables`.

### Pipeline variables

```python
from polyris import DAG

with DAG(
    "my-pipeline",
    variables={
        "run_id": "{% $states.context.Execution.Name %}",
        "environment": "prod",
    },
) as dag:
    ...
```

`variables` values may be static strings or numbers, or JSONata expressions (the `{% ... %}` form) evaluated when the run starts. Each Lambda and `@task.sfn` task of the run gets the resolved values as keys of its input.

### Date variables

A run has one date, the same for every task. It is the date the run was started for: the one you pick in the console, or, for a run an asset started, the date of that asset's fact. A run its schedule started gets the UTC day the schedule fired it for, even if it started after midnight; a run started any other way gets the UTC day it started.

The input of a Lambda or `@task.sfn` task also carries the run's date in several forms; a variable of yours with the same name wins. A Glue job, an ECS task or a Batch job gets the run's date as the environment variable `POLYRIS_RUN_DATE` (a Glue job: the argument `--POLYRIS_RUN_DATE`). For a run dated 2025-07-25:

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
| `partition_key` | `"2025-07-25"`: the same as `current_date` |
| `PARTITION_ARG` | the run's own `PARTITION_ARG`, or `current_date` |

A run of an hourly, weekly or monthly partition has that partition's key as `current_date` (`"2025-07-25T06"`, `"2025-W30"`, `"2025-07"`). Every other form is that of the partition's first day: the day of the hour, the Monday of the week, the 1st of the month.

---

## Complete example

```python
import os

from polyris import DAG, Asset, task

STAGE = os.environ.get("POLYRIS_STAGE", "dev")   # polyris-deploy --stage sets it

raw_data = Asset("raw/acme", group="acme")
processed = Asset("processed/acme", group="acme")

with DAG(
    dag_id="acme-etl",
    schedule="@daily",
    description="Acme ETL pipeline",
    tags=["production", "acme"],
) as dag:

    @task.sfn(arn=f"arn:aws:states:us-east-1:123456789012:stateMachine:scrape-{STAGE}")
    def scrape():
        """Scrape product data."""

    @task.sfn(
        arn=f"arn:aws:states:us-east-1:123456789012:stateMachine:process-{STAGE}",
        outlets=[raw_data],
        retries=2,
    )
    def process():
        """Process raw data."""

    @task.glue_job(job_name=f"transform-{STAGE}", outlets=[processed])
    def transform():
        """Transform to final format."""

    @task.lambda_function(function_name=f"notify-{STAGE}", trigger_rule="all_done")
    def notify():
        """Send a completion message, whatever the outcome."""

    scrape() >> process() >> transform() >> notify()

# Deploy: polyris-deploy --stage dev
```

Commands to validate, preview and deploy a pipeline: [CLI.md](../reference/CLI.md).
