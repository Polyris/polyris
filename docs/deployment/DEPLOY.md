# polyris-deploy — CloudFormation Pipeline Deployment

CloudFormation-based pipeline deployment. AWS manages state natively.

- No external tools needed beyond AWS CLI
- Same workflow as `sam deploy` for shared infra

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
polyris-deploy --destroy

# Multi-DAG file — deploy specific DAG
polyris-deploy --select acme-daily

# Bulk: every subdirectory of the current directory
polyris-deploy --all

# Bulk: only the listed subdirectories
polyris-deploy --only acme/daily acme/hourly

# Bulk + destroy, bulk + --select — see CLI.md for the full combination table
polyris-deploy --destroy --all
```

See [CLI.md](../reference/CLI.md) for full options reference, including the
bulk-mode (`--all`/`--only`) discovery rules and failure-handling behavior.

## What it deploys

For each pipeline:
- `AWS::StepFunctions::StateMachine` — the pipeline
- `AWS::Logs::LogGroup` — CloudWatch logs
- `AWS::Scheduler::Schedule` — EventBridge Scheduler (if `schedule` is a cron/rate string; not created for asset-triggered or `schedule=None` pipelines)

State is managed by CloudFormation — create, update, and delete handled automatically.

## Prerequisites

1. Shared infra deployed: `sam deploy` (writes SSM parameters)
2. AWS credentials configured: `aws configure`
3. polyris installed: `pip install polyris`

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

