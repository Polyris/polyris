# polyris Python DSL Reference

## Overview

polyris provides a Python DSL for defining data pipelines that compile to AWS
Step Functions. Use `@task` decorators, the `>>` dependency operator, and a
`DAG()` context manager to describe pipelines — they execute as Step Functions
state machines, with no scheduler or worker pool to operate.

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
| `catchup` | bool | No | Enable backfill (default: True) |
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

### Step Function Task

```python
@task.sfn(
    arn="arn:aws:states:us-east-1:123456789:stateMachine:my-sfn",
    execution_timeout=timedelta(hours=1),
    retries=2,
    wait_before=60,
    trigger_rule="all_success",
    outlets=[my_asset]
)
def my_task():
    pass
```

### Lambda Task

```python
@task.lambda_(
    function_name="my-function",
    payload={"key": "value"}
)
def process_data():
    pass
```

### Glue Task

```python
@task.glue(
    job_name="my-etl-job",
    arguments={"--key": "value"},
    worker_type="G.1X",
    number_of_workers=2
)
def etl_job():
    pass
```

### ECS Task

```python
@task.ecs(
    cluster="my-cluster",
    task_definition="my-task:1",
    launch_type="FARGATE",          # FARGATE requires at least one subnet
    subnets=["subnet-xxx"],
    security_groups=["sg-xxx"],
    assign_public_ip="ENABLED",     # ENABLED for a public subnet without a NAT gateway
    container_overrides={
        "containerOverrides": [{
            "name": "main",
            "command": ["python", "script.py"]
        }]
    }
)
def container_job():
    pass
```

`NetworkConfiguration` is sent only when `subnets` are provided; EC2 launch type
with `bridge`/`host` networking can omit them.

### Athena Task

```python
@task.athena(
    query_string="SELECT * FROM my_table",
    database="my_database",
    output_location="s3://bucket/output/",
    workgroup="primary"
)
def run_query():
    pass
```

Omit `output_location` to use the workgroup's enforced output location — the
wrapper then omits `ResultConfiguration` rather than sending an empty
`OutputLocation` (which `StartQueryExecution` rejects).

### EMR Task

```python
@task.emr(
    emr_cluster_id="j-XXXXXXXXXXXXX",
    emr_step={
        "Name": "Spark Job",
        "ActionOnFailure": "CONTINUE",
        "HadoopJarStep": {
            "Jar": "command-runner.jar",
            "Args": ["spark-submit", "s3://bucket/script.py"]
        }
    }
)
def spark_job():
    pass
```

### AWS Batch Task

```python
@task.batch(
    job_definition="my-job-def",
    job_queue="my-queue",
    batch_parameters={"param1": "value1"}
)
def batch_job():
    pass
```

---

## Task Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `arn` | str | - | Step Function ARN (for sfn type) |
| `execution_timeout` | timedelta | 24 hours | Max task execution time |
| `orchestration_timeout` | timedelta | same as execution_timeout | Max time waiting for dependencies |
| `retries` | int | 0 | Number of retry attempts |
| `retry_delay` | timedelta | 5 minutes | Delay between retries (base delay when backoff is on) |
| `retry_exponential_backoff` | bool | False | Double the wait each retry: `min(retry_delay·2^n, max_retry_delay)` |
| `max_retry_delay` | timedelta | none (3600s cap) | Ceiling for exponential backoff |
| `retry_jitter` | bool | False | Randomise each wait into `[base/2, base)` (avoids retry stampede) |
| `wait_before` | int | 0 | Wait N seconds before executing |
| `trigger_rule` | str | "all_success" | When to trigger task (see table below) |
| `role` | str | "same" | Cross-account role: 'acq', 'etl', 'same' |
| `outlets` | list | [] | Assets produced by this task |
| `inlets` | list | [] | Assets consumed by this task |
| `wait_for` | list | [] | Assets to wait for: `[asset]`, `[asset.within(hours=24)]`, `[asset.consecutive(days=7)]` |
| `skip_on_backfill` | bool | False | Skip this task by default during backfill |

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
    outlets=[my_asset]
)
def my_task():
    pass
```

---

## Trigger Rules

> **polyris is intervention-first, not autonomous (ADR #114).** A task that exhausts
> its retries pauses for a human decision (`retry` / `mark_success` / `skip` / `fail`)
> rather than propagating failure automatically. That's a deliberate differentiator —
> most orchestrators just fail and stop; polyris lets you fix it inline, in the same
> run. One consequence: a *confirmed* failure (resolved with `fail`) cancels the whole
> pipeline's `Parallel` before any downstream `trigger_rule` ever evaluates — so a rule
> whose only purpose is reacting to a confirmed failure can never fire. See ADR #117
> for the full reachable-state analysis.

polyris supports 5 trigger rules (ADR #117). Six additional rule names are
**rejected at validation time** (`polyris-validate` / `polyris-deploy`), each
with a specific suggestion for what to use instead.

| Rule | Description | Use Case |
|------|-------------|----------|
| `all_success` | All deps must succeed (default) | Standard ETL: extract → transform → load |
| `one_success` | At least one dep succeeded (immediate!) | Redundant sources: run if any data arrived |
| `all_done` | All deps finished (any status) | Cleanup: run after the success path, or once `all_done` propagation ships (ADR #116) |
| `all_skipped` | All deps skipped | Fallback for an entirely-optional branch |
| `none_skipped` | No dep skipped | Only proceed if nothing upstream was intentionally skipped |

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
> **`all_done` and a confirmed failure.** `all_done` on the success path (no failures
> at all) works today; reacting to a *resolved* failure specifically is a planned,
> scoped exception (ADR #116), not yet shipped — a confirmed failure still cancels this
> marker's branch before it can evaluate.
>
> **Skip cascades for `all_success` (ADR #115).** A skipped upstream blocks
> `all_success` — but only when the skip came from a *rule*
> resolving `skipped`. A **manual** skip (an operator explicitly skipping a paused task
> to unblock it) does not cascade — a human tolerating one gap shouldn't silently
> no-op an entire downstream chain.

### Examples

```python
# Redundant sources — one_success, no caveats
@task.sfn(arn=..., trigger_rule="one_success")
def merge_results():
    """Runs as soon as any upstream source succeeds — doesn't wait for the rest."""
    pass

# Cleanup that runs after the success path — all_done
@task.sfn(arn=..., trigger_rule="all_done")
def cleanup():
    """Tears down resources once every upstream has finished. Reacting to a
    *resolved failure* specifically is a planned exception (ADR #116), not yet
    shipped — today this fires reliably once the success path completes."""
    pass
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

### Pipeline Variables

```python
with DAG(
    "my-pipeline",
    variables={
        "current_date": "{% $substringBefore($now(), 'T') %}",
        "environment": "prod"
    }
) as dag:
    ...
```

### Auto Variables (in backfill)

When running backfill, these variables are auto-generated:

| Variable | Example | Description |
|----------|---------|-------------|
| `current_date` | "2025-07-25" | Execution date |
| `date_compact` | "20250725" | YYYYMMDD format |
| `year` | "2025" | Year |
| `month` | "07" | Month |
| `day` | "25" | Day |
| `day_of_week` | "friday" | Lowercase weekday |
| `previous_date` | "2025-07-24" | Day before |
| `minus_7_days` | "2025-07-18" | 7 days ago |
| `minus_30_days` | "2025-06-25" | 30 days ago |
| `is_backfill` | true | Backfill flag |

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
        skip_on_backfill=True
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
    
    @task.glue(
        job_name=f"transform-{STAGE}",
        outlets=[processed],
        trigger_rule="all_success"
    )
    def transform():
        """Transform to final format."""
        pass
    
    @task.lambda_(function_name=f"notify-{STAGE}", trigger_rule="all_done")
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
