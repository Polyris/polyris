# polyris

**Orchestration without the orchestrator**

Serverless, asset-centric data pipelines on AWS Step Functions. No scheduler, no
workers, no metadata database to run — your pipelines compile to Step Functions,
and AWS runs them. Pay per run; idle cost is near zero.

## Why polyris

- 🪂 **Nothing to run** — no scheduler, workers, or metadata DB. Step Functions
  *is* the runtime, and it scales to zero between runs.
- 🔎 **Nothing hidden** — every pipeline compiles to a Step Functions state
  machine, so each run is a visible, debuggable execution history rather than
  opaque scheduler state.
- 🧬 **Asset-centric** — pipelines declare first-class data assets and their
  dependencies, not just task graphs.
- 💸 **Pay-per-run** — a typical deployment runs ~$31/month; the floor is near
  zero because there is no always-on infrastructure.

## Features

- 🐍 **Familiar Python DSL** — `@task`, `>>` operators, `DAG()` context manager
- 🚀 **One-command deploy** — `polyris-deploy` (CloudFormation)
- 🧪 **Local testing** — Validate, dry-run, mock execution
- 🔔 **Failure notifications** — browser notifications on failure (the notify Lambda fans out to every enabled channel — no silent failures)
- ⏸️ **Intervention-first failures** — a failing task pauses for a human decision
  (retry / mark success / skip / fail) instead of just falling over — fix it inline,
  in the same run, free (ADR #114)
- 🎯 **Trigger rules** — `all_success`, `one_success`, `all_done`, and more ([details](docs/features/DSL.md#trigger-rules))
- 🔗 **Automatic data passing** — outputs flow to downstream **lambda & SFN** tasks via a DynamoDB output store (up to 350KB); service tasks (glue/ecs/…) exchange data via S3
- 📊 **Web Console** — pipelines and DAG views for every run
- 🧬 **Asset dependencies** — declare cross-pipeline asset inlets/outlets; inspect lineage from the CLI with `polyris-output --graph`
- 🔗 **Pull-based deps** — `wait_for` with freshness and consecutive checks
- 🔄 **Auto-refresh UI** — polling-based updates (3s active, 30s idle)

---

## Where to Start

| I want to... | Go to |
|---|---|
| **Try polyris without AWS** (explore DSL locally) | [Try It Now](#try-it-now) below |
| **Browse runnable examples** | [examples/](examples/) — hello-world → assets & lineage |
| **Write a pipeline** (infra already deployed) | [Quick Start](#quick-start) below |
| **Set up polyris from scratch** (blank AWS account) | [SETUP_FROM_SCRATCH.md](docs/getting-started/SETUP_FROM_SCRATCH.md) |
| **Learn step by step** with explanations | [TUTORIAL.md](docs/getting-started/TUTORIAL.md) |
| **Develop polyris itself** (fix bugs, add features) | [CONTRIBUTING.md](CONTRIBUTING.md) |
| **Troubleshoot** a problem | [TROUBLESHOOTING.md](docs/operations/TROUBLESHOOTING.md) |

---

## Try It Now

No AWS account needed. Explore the DSL, validate pipelines, generate Step Functions JSON — all locally.

```bash
git clone https://github.com/Polyris/polyris
cd polyris
pip install -e .
polyris-init my-pipeline --local
cd my-pipeline
polyris-validate              # Validate pipeline
polyris-validate -v           # Verbose: tasks, deps, ASL preview
polyris-output --json         # Full Step Functions JSON
polyris-output --mermaid      # Generate diagram
polyris-output --graph        # Show DAG as ASCII graph
```

Or browse [examples/](examples/) for 15 small, self-contained pipelines — hello-world through assets and lineage.

Edit `dag.py` to experiment with task types, dependencies, trigger rules, and assets. When ready to deploy, see [Quick Start](#quick-start).

---

## Quick Start

> Assumes shared infrastructure is already deployed. Starting from scratch? See [SETUP_FROM_SCRATCH.md](docs/getting-started/SETUP_FROM_SCRATCH.md).

### 1. Install

```bash
git clone https://github.com/Polyris/polyris
cd polyris
pip install -e .   # installs polyris-deploy, polyris-validate, polyris-output, polyris-init
cd ..              # back to your workspace
```

### 2. Configure (config.py)

Create `config.py` in your pipelines workspace (not inside the polyris repo):

```python
# config.py — in your pipelines workspace root (alongside your pipeline directories)
ENVIRONMENTS = {
    "dev": {
        "namespace": "mycompany",
        "stage": "dev",
        "region": "us-east-1",
        # Cross-account role shortcuts (optional, used as role="etl" in tasks)
        "roles": {
            "etl": "arn:aws:iam::333333333333:role/cross-account-pipeline",
        },
    },
}

DEFAULT_STAGE = "dev"
```

### 3. Create pipeline

```bash
polyris-init my-pipeline
```

This creates `my-pipeline/` with a working `dag.py`. Edit the task names and ARNs, then deploy with `polyris-deploy`.

Or create manually — `my-pipeline/dag.py`:

```python
from polyris import DAG, task, Asset

processed = Asset(name="my-pipeline/processed")

with DAG(
    dag_id="my-pipeline",
    schedule="@daily",
) as dag:

    # Same account — just the ARN
    @task.sfn(arn="arn:aws:states:us-east-1:111111111111:stateMachine:extract")
    def extract(): pass

    # Cross-account — role assumes into another account to run the SFN
    @task.sfn(
        arn="arn:aws:states:us-east-1:222222222222:stateMachine:transform",
        role="arn:aws:iam::222222222222:role/cross-account-pipeline",
    )
    def transform(): pass

    # role from config.py ENVIRONMENTS roles dict
    @task.sfn(
        arn="arn:aws:states:us-east-1:333333333333:stateMachine:load",
        role="etl",  # resolves from config.py ENVIRONMENTS["dev"]["roles"]["etl"]
        outlets=[processed],
    )
    def load(): pass

    extract() >> transform() >> load()

# Deploy: polyris-deploy --stage $STAGE
```

<details>
<summary>Advanced: multi-stage pipeline (one file for dev + prod)</summary>

```python
from polyris import DAG, task, config
import os

STAGE = os.environ.get("POLYRIS_STAGE", "dev")

with DAG("my-pipeline", schedule="@daily") as dag:

    # ARN uses STAGE to target correct environment
    @task.sfn(arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-extract")
    def extract(): pass

    # Cross-account shorthand works the same way:
    #   role="etl" → resolves from config.py ENVIRONMENTS["dev"]["roles"]["etl"]
    @task.sfn(arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-load", role="etl")
    def load(): pass

    extract() >> load()

# Deploy: polyris-deploy --stage $STAGE
```

Deploy: `polyris-deploy --stage dev`
</details>

### 4. Deploy

```bash
polyris-deploy
```

### 5. Open Console

```bash
# Replace `polyris-dev` with your stack name (the `stack_name` in samconfig.toml).
aws cloudformation describe-stacks --stack-name polyris-dev --query "Stacks[0].Outputs[?OutputKey=='ConsoleUiUrl'].OutputValue" --output text
```

Or open the AWS CloudFormation console → your stack → Outputs tab → `ConsoleUiUrl`.

### 6. Trigger a run

The pipeline runs on its schedule, but you can trigger it manually right away from the console: open your pipeline → click **Run**. The run appears in the DAG view within seconds.

Pipeline runs daily at midnight, with automatic retries and alerts. Something broken? See [TROUBLESHOOTING.md](docs/operations/TROUBLESHOOTING.md).

---

## 🧪 Local Testing

Test pipelines without deploying:

```python
from polyris.local import validate, dry_run, run

# Validate DAG structure
validate(dag)

# Show execution plan
dry_run(dag)

# Mock execution
result = run(dag, mock=True)
print(result.summary())  # ✅ 3 succeeded, ❌ 0 failed
```

---

## Notifications

Failure delivers an **in-app browser notification** automatically — no
configuration required.

> `DAG` has **no `alerts=` argument** — alert config is not part of the DSL
> (ADR #103). Passing `alerts={...}` raises a `TypeError`.

```python
# No alert config in the DAG — just define the pipeline.
with DAG("pipeline", schedule="@daily") as dag:
    ...
```

## Task Types

```python
# Step Function
@task.sfn(arn="arn:aws:states:...")
def my_task(): pass

# Lambda
@task.lambda_(function_name="my-function")
def process(): pass

# Glue
@task.glue(job_name="my-etl-job")
def etl(): pass

# ECS (Fargate)
@task.ecs(cluster="my-cluster", task_definition="my-task")
def container_job(): pass

# Athena
@task.athena(query_string="SELECT * FROM table", database="my_db")
def query(): pass

# EMR
@task.emr(emr_cluster_id="j-XXXXX", emr_step={...})
def spark_job(): pass

# AWS Batch
@task.batch(job_definition="my-job", job_queue="my-queue")
def batch_job(): pass
```

---

## Dependencies

```python
# Sequential
a >> b >> c

# Fan-out (one to many)
a >> [b, c, d]

# Fan-in (many to one)
[a, b, c] >> d

# Function call style
result = task_a()
task_b(result)
```

---

## Schedule Options

```python
# Time-based
DAG(schedule="@daily")                    # Midnight UTC
DAG(schedule="@hourly")                   # Every hour
DAG(schedule="cron(0 8 * * ? *)")         # 8:00 UTC daily
DAG(schedule="rate(6 hours)")             # Every 6 hours

# Asset-triggered
DAG(schedule=[processed_data])            # When asset ready
DAG(schedule=[asset_a & asset_b])         # When ALL ready (AND)
DAG(schedule=[asset_a | asset_b])         # When ANY ready (OR)

# Manual only
DAG(schedule=None)
```

---

## Asset-Based Orchestration

> **⚠️ Experimental (v0.93.0).** Assets are experimental — the API may change and
> there's no visual asset console yet (`polyris-output --graph` shows lineage). Not
> recommended for production yet. See [docs/features/ASSETS.md](docs/features/ASSETS.md).
> <!-- EXPERIMENTAL-ASSETS: remove when assets graduate to stable. -->

Cross-pipeline dependencies without hardcoded references:

```python
# Producer pipeline
processed = Asset(name="processed/acme")

with DAG("acme-daily", schedule="@daily") as dag:
    @task.sfn(arn=..., outlets=[processed])
    def process(): pass
```

```python
# Consumer pipeline (triggered by asset)
processed = Asset(name="processed/acme")

with DAG("feeds", schedule=[processed]) as dag:
    @task.sfn(arn=...)
    def build_feeds(): pass
```

```python
# Pull-based dependencies (wait_for)
# Task waits for asset freshness before executing
daily_complete = Asset("acme/daily-complete")
weekly_complete = Asset("acme/weekly-complete")

with DAG("acme-weekly", schedule="cron(0 22 ? * SUN *)") as dag:
    @task.sfn(
        arn=...,
        wait_for=[daily_complete.consecutive(days=7)],  # Wait for 7 daily runs
        outlets=[weekly_complete]
    )
    def mark_weekly_complete(): pass
```

---

## Web Console

Access the console at your CloudFront URL. Features:

| View | Description |
|------|-------------|
| **🔀 DAG** | Interactive graph visualization (React Flow) |
| **📋 Tasks** | All task instances across pipelines |
| **🏃 Runs** | All pipeline runs with filtering |

### Task Actions
- **Skip** — Mark task as skipped, continue pipeline
- **Fail** — Mark task as failed, continue pipeline
- **Stop** — Force stop running task
- **Restart** — Retry failed task
---

## CLI Commands

Run from the pipeline directory:

```bash
# Validate pipeline
polyris-validate

# Validate with details
polyris-validate -v

# Validate all pipelines in project
polyris-validate --all

# Generate Step Functions JSON
polyris-output --json

# Generate Mermaid diagram
polyris-output --mermaid

# Show DAG as ASCII graph
polyris-output --graph

# Deploy pipeline
polyris-deploy
polyris-deploy --stage prod --profile my-profile

# Register pipeline in DynamoDB (manual)
polyris-register --name my-pipeline
```

Full reference: [docs/reference/CLI.md](docs/reference/CLI.md)

---

## Project Structure

```
├── pipelines/                    # Pipeline definitions
│   ├── config.py                 # Shared config: ENVIRONMENTS, DEFAULT_STAGE
│   └── my-pipeline/
│       └── dag.py                # Pipeline definition
│
├── sam/                          # Shared infrastructure (SAM/CloudFormation)
│   ├── template.yaml             # SAM template (all AWS resources)
│   ├── samconfig.toml            # Deploy configuration
│   ├── samconfig.toml.example    # Example config
│   ├── lambdas/                  # 6 Lambda functions
│   └── sfn_templates/            # 13 SFN template files (16 SFNs total incl. 3 test)
│
├── polyris/                      # Python DSL library
│   ├── dag.py                    # DAG class
│   ├── task.py                   # Task decorators
│   ├── assets.py                 # Asset definitions
│   └── generators.py             # ASL JSON generation
│
├── ui/                           # Web Console (React 19 + Next.js 16)
│   ├── deploy.sh                 # UI deploy script (S3 + CloudFront)
│   └── src/
│       ├── app/                  # Next.js App Router
│       └── components/           # React components
│
└── tests/                        # Test suite
```

---

## Deploy Infrastructure

```bash
cd sam
sam build && sam deploy
```

See [SETUP_FROM_SCRATCH.md](docs/getting-started/SETUP_FROM_SCRATCH.md) for full setup or [QUICKSTART.md](docs/getting-started/QUICKSTART.md) for fast path.

---

## Documentation

| Document | Description |
|----------|-------------|
| [QUICKSTART.md](docs/getting-started/QUICKSTART.md) | 5-minute setup guide |
| [TUTORIAL.md](docs/getting-started/TUTORIAL.md) | From zero to production guide |
| [PROJECT_STRUCTURE.md](docs/getting-started/PROJECT_STRUCTURE.md) | Repository layouts, CI/CD |
| [DSL.md](docs/features/DSL.md) | Python DSL reference |
| [ASSETS.md](docs/features/ASSETS.md) | Asset-based orchestration |
| [ASSET_PULL_FEATURE.md](docs/features/ASSET_PULL_FEATURE.md) | wait_for / pull-based assets |
| [authentication.md](docs/features/authentication.md) | Cognito auth setup |
| [api-tokens.md](docs/features/api-tokens.md) | API tokens (PAT) for scripts/CI — 🔒 Team (OSS: use a Cognito access token) |
| [LOCAL_TESTING.md](docs/tools/LOCAL_TESTING.md) | Local testing (validate, dry_run, mock) |
| [REGISTRATION.md](docs/tools/REGISTRATION.md) | Pipeline registration (CLI, auto) |
| [API.md](docs/operations/API.md) | REST API reference (27 free endpoints; 63 in the full build) |
| [UI.md](docs/operations/UI.md) | Web Console guide |
| [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) | System architecture, diagrams |
| [STEP_FUNCTIONS.md](docs/architecture/STEP_FUNCTIONS.md) | ASL patterns and helpers |
| [BACKEND.md](docs/architecture/BACKEND.md) | Backend implementation details |
| [DESIGN_DECISIONS.md](docs/reference/DESIGN_DECISIONS.md) | Key design decisions |

---

## Cost

| | Managed orchestrators | polyris |
|---|----------------|---------|
| **Base cost** | ~$300+/month | $0 |
| **Per pipeline run** | $0 (included) | ~$0.01 |
| **8 tasks, 1x/day, 30 days** | ~$300+ | ~$0.50 |
| **Scaling** | Manual | Automatic |

---

## Requirements

- Python 3.11+
- AWS Account

```bash
pip install -e .          # pipeline development
pip install -e ".[dev]"   # polyris development (adds pytest, ruff, mypy)
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

Quick start:
```bash
make test    # Run all tests
make check   # Lint + sync + test (before PR)
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
