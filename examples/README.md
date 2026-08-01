# polyris examples

Self-contained pipelines that build up the DSL. Every one runs **locally with no
AWS account** — explore the DSL, see the generated Step Functions definition, and
iterate before you deploy anything.

The first few are minimal intros; the rest each exercise a slice of the full
feature surface (task types, branching, direct steps, DAG/task settings, and a
composite end-to-end run).

| Example | Concept |
|---|---|
| [`01_single_task`](01_single_task/dag.py) | The smallest pipeline — one scheduled task |
| [`02_linear_chain`](02_linear_chain/dag.py) | `extract → transform → load`; passing data between tasks |
| [`03_fan_in`](03_fan_in/dag.py) | Parallel tasks + a `trigger_rule` (`all_done`) |
| [`04_task_types`](04_task_types/dag.py) | Six AWS task types: `sfn`, `lambda_`, `glue`, `athena`, `ecs`, `batch` + `default_args` + cross-account `role` (`emr` is supported too but omitted here — it needs a live cluster) |
| [`05_chain_cross_downstream`](05_chain_cross_downstream/dag.py) | Fan-out / fan-in, trigger rules, `chain()` / `cross_downstream()` |
| [`06_direct_steps`](06_direct_steps/dag.py) | Direct service steps (`Wait`, `Pass`, `SNS`, `SQS`, `S3`), `wait_before`, `rate(...)`, `variables` |
| [`07_dag_and_task_settings`](07_dag_and_task_settings/dag.py) | Retries + backoff, timeouts, `skip_on_backfill`, `catchup`, concurrency limits |
| [`08_composite_example`](08_composite_example/dag.py) | A plausible daily analytics pipeline combining several services end to end — not a single feature, the full picture |
| [`09_trigger_rules`](09_trigger_rules/dag.py) | All five supported `trigger_rule` conditions side by side (ADR #117) |
| [`10_no_schedule`](10_no_schedule/dag.py) | An on-demand pipeline with **`schedule=None`** (manual trigger only) |
| [`11_assets_outlets_inlets`](11_assets_outlets_inlets/dag.py) | Declaring asset `outlets`/`inlets` on tasks *(experimental)* |
| [`12_assets_schedule_trigger`](12_assets_schedule_trigger/dag.py) | `schedule=[asset]` — a pipeline triggered by another pipeline's asset (push model) *(experimental)* |
| [`13_assets_wait_for`](13_assets_wait_for/dag.py) | `wait_for=[asset]` — a task pauses for asset freshness (pull model) *(experimental)* |
| [`14_assets_and_or`](14_assets_and_or/dag.py) | AND vs OR trigger logic with two independent assets *(experimental)* |
| [`15_assets_every_event`](15_assets_every_event/dag.py) | `AssetAny([x])` for one asset — trigger on every event, no day dedup *(experimental)* |

## Run one locally

```bash
pip install -e .                 # from the repo root, once
cd examples/01_single_task

polyris-validate                 # is the DAG valid?
polyris-validate -v              # verbose: tasks, dependencies, ASL preview
polyris-output --graph           # the DAG as an ASCII graph
polyris-output --mermaid         # a Mermaid diagram you can paste anywhere
polyris-output --json            # the full Step Functions definition (ASL)
```

Each `@task.sfn` invokes an existing Step Functions state machine, so the ARNs
in these files are placeholders — they're fine for local validation; replace
them with your own before deploying.

## Deploy one for real

```bash
cd examples/02_linear_chain
polyris-deploy                   # builds + deploys via CloudFormation
```

See the top-level [README](../README.md) and
[docs/getting-started](../docs/getting-started/) for setup, the tutorial, and the
full DSL reference.
