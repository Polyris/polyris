# polyris examples

Self-contained pipelines that build up the DSL. Every one runs **locally with no AWS account**: explore the DSL, see the generated Step Functions definition, and iterate before you deploy anything.

The first few are minimal intros; each of the rest covers one part of the DSL (task types, branching, direct steps, DAG and task settings, assets, data passing), and one combines several services end to end.

| Example | Concept |
|---|---|
| [`01_single_task`](01_single_task/dag.py) | The smallest pipeline — one scheduled task |
| [`02_linear_chain`](02_linear_chain/dag.py) | `extract → transform → load`; passing data between tasks |
| [`03_fan_in`](03_fan_in/dag.py) | Parallel tasks + a `trigger_rule` (`all_done`) |
| [`04_task_types`](04_task_types/dag.py) | Six AWS task types: `sfn`, `lambda`, `glue`, `athena`, `ecs`, `batch` + `default_args` (`emr` is supported too but omitted here — it needs a live cluster) |
| [`05_chain_cross_downstream`](05_chain_cross_downstream/dag.py) | Fan-out / fan-in, trigger rules, `chain()` / `cross_downstream()` |
| [`06_direct_steps`](06_direct_steps/dag.py) | Direct service steps (`Wait`, `Pass`, `SNS`, `SQS`, `S3`), `wait_before`, `rate(...)`, `variables` |
| [`07_dag_and_task_settings`](07_dag_and_task_settings/dag.py) | Retries + backoff, timeouts |
| [`08_composite_example`](08_composite_example/dag.py) | A plausible daily analytics pipeline combining several services end to end — not a single feature, the full picture |
| [`09_trigger_rules`](09_trigger_rules/dag.py) | All nine `trigger_rule` conditions side by side |
| [`10_no_schedule`](10_no_schedule/dag.py) | An on-demand pipeline with **`schedule=None`** (manual trigger only) |
| [`11_assets_outlets_inlets`](11_assets_outlets_inlets/dag.py) | Declaring asset `outlets`/`inlets` on tasks *(experimental)* |
| [`12_assets_schedule_trigger`](12_assets_schedule_trigger/dag.py) | `schedule=[asset]` — a pipeline triggered by another pipeline's asset (push model) *(experimental)* |
| [`13_assets_wait_for`](13_assets_wait_for/dag.py) | `wait_for=[asset]` — a task pauses for asset freshness (pull model) *(experimental)* |
| [`14_assets_and_or`](14_assets_and_or/dag.py) | AND vs OR trigger logic with two independent assets *(experimental)* |
| [`15_assets_every_event`](15_assets_every_event/dag.py) | `AssetAny([x])` for one asset — trigger on every event, no day dedup *(experimental)* |
| [`16_xcom_showcase`](16_xcom_showcase/dag.py) | Every XCom path in one pipeline — `xcom.get()`, `xcom.push()`, primitive return values, typed errors, the console's per-upstream input cards |
| [`17_all_task_types_xcom`](17_all_task_types_xcom/dag.py) | End-to-end XCom smoke — every task type (Lambda, Glue Spark, Glue pythonshell, Athena, ECS, Batch, SFN) reads upstream and writes output |

## Run one locally

With the CLI installed ([README → Install](../README.md#install)), clone this repository (or download its source archive) and `cd` into an example; nothing else to install:

```bash
cd examples/01_single_task

polyris-validate                 # is the DAG valid?
polyris-validate -v              # verbose: schedule and task count
polyris-output --graph           # the DAG as an ASCII graph
polyris-output --mermaid         # a Mermaid diagram you can paste anywhere
polyris-output --json            # the Step Functions definition and its task specs
```

Each task calls an existing AWS resource. The ARNs and names in these files point at the resources of [testing-infra](testing-infra/README.md) in account `000000000000`: they're fine for local validation; deploy that stack or replace them with your own before deploying.

## Deploy one for real

First set `stack_name`, `namespace` and `account_id` in [config.py](config.py) to your platform's: `polyris-deploy` refuses an account other than `account_id`.

```bash
cd examples/02_linear_chain
polyris-deploy                   # builds + deploys via CloudFormation
```

Setup: [QUICKSTART.md](../docs/getting-started/QUICKSTART.md). Every DSL feature: [DSL.md](../docs/features/DSL.md).
