# polyris-deploy — CloudFormation Pipeline Deployment

CloudFormation-based pipeline deployment. AWS manages state natively.

- Python only: deploys through a CloudFormation change set with boto3 — no AWS CLI or SAM CLI
- Finds the platform stack installed by `polyris-up` on its own; `config.py` is optional

## Usage

```bash
# Deploy from pipeline directory (uses config.py defaults)
cd pipelines/acme/daily
polyris-deploy

# Deploy to specific stage
polyris-deploy --stage prod

# Override AWS profile
polyris-deploy --profile my-aws-profile

# Stage + profile
polyris-deploy --stage prod --profile my-aws-profile

# Preview without deploying
polyris-deploy --dry-run

# Remove pipeline
polyris-destroy

# Multi-DAG file — deploy specific DAG
polyris-deploy --select acme-daily

# Bulk: every folder in the current directory (hidden ones and caches aside)
polyris-deploy --all

# Bulk: only the listed subdirectories
polyris-deploy --only acme/daily acme/hourly

# Bulk + destroy, bulk + --select — see CLI.md for the full combination table
polyris-destroy --all
```

See [CLI.md](../reference/CLI.md) for full options reference, including the
bulk-mode (`--all`/`--only`) discovery rules and failure-handling behavior.

## What it deploys

For each pipeline:
- `AWS::StepFunctions::StateMachine` — the pipeline
- `AWS::Logs::LogGroup` — CloudWatch logs
- `AWS::Scheduler::Schedule` — EventBridge Scheduler (if `schedule` is a cron/rate string; not created for asset-triggered or `schedule=None` pipelines)

State is managed by CloudFormation — create, update, and delete handled automatically.

Before it creates the change set, `polyris-deploy` writes each task's spec to the platform's `pipeline-specs` table, reads the version back and checks it (its count, each text's hash, its fingerprint). A spec it cannot write or confirm stops the deploy before the stack changes; `--dry-run` writes nothing and prints the version it would write. After the stack deploys, it records the deploy time and the pipeline's format in the registry. A pipeline beyond a limit of [DSL.md § Pipeline limits](../features/DSL.md#pipeline-limits) is refused before anything reaches AWS.

## Prerequisites

1. The platform installed: `polyris-up` ([QUICKSTART.md](../getting-started/QUICKSTART.md)). `polyris-deploy` reads its CloudFormation outputs.
2. AWS credentials for the same account and region (standard credential chain, or `--profile` / `--region`), with the deploy permissions of [IAM_PERMISSIONS.md](IAM_PERMISSIONS.md#deploy--per-pipeline-polyris-deploy), `pipeline-specs` included.

Which platform stack: `--stack` > `stack_name` in `config.py` > the one stack tagged by `polyris-up` in the region. Several tagged stacks and no name is an error that asks for `--stack`. Which region: `--region` > `POLYRIS_REGION` / `AWS_REGION` > `config.py` > your AWS profile's region.

## Pipeline file

Standard `dag.py` format:

```python
from polyris import DAG, task
import os

STAGE = os.environ.get("POLYRIS_STAGE", "dev")

with DAG("acme-daily", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:...")
    def my_task(): pass
    my_task()

# No deploy() call needed — polyris-deploy reads the DAG automatically
```

