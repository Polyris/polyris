# Explore polyris locally — a hands-on tutorial

Learn the polyris DSL without touching AWS. In ~10 minutes you'll install the
SDK, generate a pipeline, edit it, and inspect the ASL / DAG / Mermaid output
the tools produce. When you're done, you can deploy for real by following
[QUICKSTART.md](QUICKSTART.md).

> **⚠️ Experimental.** Assets (`Asset`, `outlets`, `inlets`, `wait_for`,
> asset-triggered `schedule`) are experimental — the API may change. Not
> recommended for production yet. Silence the runtime warning with
> `warnings.filterwarnings("ignore", category=polyris.ExperimentalWarning)`.
> <!-- EXPERIMENTAL-ASSETS: remove when assets graduate to stable. -->

## Prerequisites

**Python 3.11+** — that's it. Everything in this tutorial runs locally.

## Step 1: Install polyris (2 min)

```bash
mkdir my-pipelines && cd my-pipelines
python3 -m venv .venv
source .venv/bin/activate
pip install polyris
python -c "from polyris import DAG, task; print('✓ polyris installed')"
```

## Step 2: Generate a pipeline (1 min)

```bash
polyris-init my-first-pipeline --local
cd my-first-pipeline
```

You now have `dag.py` — a working pipeline definition with placeholder ARNs.
It's a linear ETL (extract → transform → load) using three `@task.sfn` tasks.

## Step 3: Inspect the DAG (2 min)

Try each of these — every polyris pipeline is inspectable without AWS:

```bash
polyris-validate              # Validate the pipeline (must pass)
polyris-validate -v           # Verbose: tasks, deps, ASL preview
polyris-output --json         # Full Step Functions JSON
polyris-output --mermaid      # Mermaid diagram source
polyris-output --graph        # DAG as ASCII graph
```

The `--json` output is the exact ASL that would be deployed. `--mermaid` gives
you a diagram you can paste into any Markdown renderer. `--graph` is the
fastest way to sanity-check the shape.

## Step 4: Edit and re-inspect (5 min)

Open `dag.py` and replace the linear chain with a fan-out. Add an asset while
you're at it:

```python
from polyris import DAG, task, Asset

processed = Asset("my-data/processed")

with DAG(
    dag_id="my-first-pipeline",
    schedule="@daily",
) as dag:

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:extract")
    def extract():
        pass

    @task.sfn(
        arn="arn:aws:states:us-east-1:123456789012:stateMachine:transform",
        retries=2,                    # Retry on failure
        trigger_rule="all_success",   # Only run if extract succeeded
    )
    def transform():
        pass

    @task.sfn(
        arn="arn:aws:states:us-east-1:123456789012:stateMachine:load",
        outlets=[processed],          # Emits an asset event on success
    )
    def load():
        pass

    extract() >> [transform(), load()]   # Fan-out from extract
```

Re-run `polyris-validate -v` to see how the shape changed. Try
`polyris-output --graph` — you should see two children under `extract`.

At this point you understand what polyris does at authoring time: describe a
DAG in Python, get ASL / diagrams / lineage inspection out. Nothing has
touched AWS.

## Next: deploy for real

- **Deploy end-to-end** — [QUICKSTART.md](QUICKSTART.md) is the how-to that
  takes you from an empty AWS account to a running pipeline in the Console.
- **DSL reference** — [DSL.md](../features/DSL.md) — every task type
  (`sfn`, `lambda_function`, `glue_job`, `ecs_task`, `athena_query`,
  `emr_step`, `batch_job`), every common parameter, every trigger rule.
- **Pass data between tasks** — [DATA_PASSING.md](../features/DATA_PASSING.md).
- **Configuration** — [CONFIGURATION.md](../reference/CONFIGURATION.md).

## Quick reference (DSL syntax)

```python
from polyris import DAG, task, Asset

# Dependencies
a >> b >> c           # Sequential
a >> [b, c]           # Fan-out
[a, b] >> c           # Fan-in

# Schedules
schedule="@daily"     # cron preset
schedule="cron(0 8 * * ? *)"
schedule=[asset]      # Asset-triggered
schedule=None         # Manual only

# Common task parameters (all task types)
retries=2
trigger_rule="all_success"   # or one_success / all_done / all_skipped / none_skipped
execution_timeout=timedelta(hours=1)
outlets=[my_asset]
inlets=[other_asset]
wait_for=[other_asset.within(hours=24)]
```
