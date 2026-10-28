# Pipeline Registration

## Overview

polyris pipelines must be registered in DynamoDB tables for:
- **UI discovery** - Console shows pipelines from `pipeline_registry`
- **Asset triggers** - `notify_asset_consumers` SFN queries `asset_subscriptions`

Registration happens automatically in three ways:

| Method | When | Automatic | Latency |
|--------|------|-----------|---------|
| **polyris-deploy lifecycle** | `polyris-deploy` / `polyris-destroy` | Yes | ~2-3 seconds |
| **Pipeline run** | Every execution (self-healing) | Yes | 0 (inline) |
| **CLI** | Manual `polyris-register` command | No | ~2-3 seconds |

## polyris-deploy Lifecycle Registration

When you run `polyris-deploy`, it does the following after the CloudFormation
stack succeeds:

```
polyris-deploy
    │
    ▼
CloudFormation deploy (creates/updates the pipeline StateMachine)
    │
    ▼
DynamoDB UpdateItem on pipeline_registry
    └──► registered_at = <now>   ← "Deployed at" timestamp in the Console
    │
    ▼
SFN StartExecution(register_only=true) — fire-and-forget
    │
    ▼
Register_Pipeline state (UpdateItem)
    ├──► sfn_arn, tasks, dag, schedule, description, ...
    └──► asset_subscriptions (if asset-triggered)
    │
    ▼
Save_DAG_Snapshot state
    └──► tokens_table (dag_snapshot::{execution_name}, TTL 120 days)
```

`registered_at` is written **once**, by `polyris-deploy`, and is never
overwritten by pipeline runs. The Console's "Deployed at X" reflects the last
actual deploy, not the last run.

**On destroy** (`polyris-destroy`): deletes the CloudFormation stack
(removes the pipeline StateMachine and EventBridge schedule). DynamoDB entries
(`pipeline_registry`, `asset_subscriptions`) are not removed automatically —
delete the items directly via the AWS Console / CLI if needed.

## Self-Healing Registration

Every pipeline run includes registration as the first step:

```
Pipeline Start
    │
    ▼
Register_Pipeline state
    │
    ├──► pipeline_registry
    └──► asset_subscriptions (if asset-triggered)
    │
    ▼
Save_DAG_Snapshot state
    │
    └──► tokens_table (dag_snapshot::{execution_name}, TTL 120 days)
    │
    ▼
Check_Register_Only
    │
    ├── true  → Success (exit)
    └── false → Run_All_Tasks
```

This ensures metadata stays current even if DynamoDB items were deleted manually
or subscriptions expired (TTL).

Note: the self-healing run does **not** update `registered_at`. Only
`polyris-deploy` writes that field — so "Deployed at" always reflects the last
actual deploy, not the last run.

## Manual Registration (CLI)

Use `polyris-register` to register without running tasks:

```bash
# Install (replace <VERSION> with a real tag from
# https://github.com/Polyris/polyris/tags, e.g. v1.0.1)
pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"

# Register by ARN
polyris-register arn:aws:states:us-east-1:123456789:stateMachine:my-pipeline

# Register by name
polyris-register --name my-pipeline --region us-east-1
```

### Authentication

Uses standard AWS credential chain (same as AWS CLI):

```bash
# Default credentials (env vars, instance profile)
polyris-register --name my-pipeline

# AWS profile from ~/.aws/credentials or ~/.aws/config
polyris-register --name my-pipeline --profile prod

# AWS_PROFILE environment variable
AWS_PROFILE=prod polyris-register --name my-pipeline

# Assume IAM role
polyris-register --name my-pipeline --role-arn arn:aws:iam::123:role/deploy

# Cross-account (profile + role)
polyris-register --name my-pipeline --profile dev --role-arn arn:aws:iam::456:role/prod
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Pipeline name (alternative to ARN) |
| `--region` | `-r` | AWS region (default: us-east-1) |
| `--profile` | `-p` | AWS profile from ~/.aws/credentials or ~/.aws/config |
| `--role-arn` | | IAM role ARN to assume |
| `--namespace` | | Namespace prefix for pipeline search |
| `--json` | | Output result as JSON |

### Example Output

```
$ polyris-register --name feeds-pipeline --profile prod

Looking for pipeline 'feeds-pipeline' in us-east-1...
   Found: arn:aws:states:us-east-1:123:stateMachine:polyris-prod-feeds-pipeline

Registering pipeline...
   Using profile: prod

Registration triggered.
   Execution: arn:aws:states:us-east-1:123:execution:feeds-pipeline:reg-abc123
   Started: 2026-01-27T13:00:00

   Pipeline will be registered in:
   - pipeline_registry (for UI discovery)
   - asset_subscriptions (for asset triggers)
```

> **Note:** `polyris-register` does **not** write `registered_at`. It triggers the
> pipeline's register-only SFN execution, which refreshes runtime metadata (tasks, dag,
> schedule, etc.) but never touches deploy-owned fields. If you use `polyris-register`
> as a recovery step instead of re-running `polyris-deploy`, the Console's "Deployed at"
> will remain at the timestamp of the last actual deploy — or absent entirely if the
> pipeline was never deployed before.

## When to Use Manual Registration

1. **After deploy, before first asset** - Asset-triggered pipelines need subscriptions before assets arrive
2. **Testing** - Verify registration works without running tasks
3. **Recovery** - Re-register after manual DynamoDB cleanup
4. **CI/CD** - Include in deployment scripts as safety net

### CI/CD Example

```yaml
# GitHub Actions
deploy:
  steps:
    - run: polyris-deploy
    
    # Optional safety net: polyris-deploy already registers inline, but
    # an explicit call here re-triggers registration if the SFN step failed.
    - run: |
        polyris-register \
          --name ${{ env.PIPELINE_NAME }} \
          --region ${{ env.AWS_REGION }} \
          --profile deploy
```

## Troubleshooting

### Pipeline not appearing in UI

1. Check `pipeline_registry` table in DynamoDB
2. Run `polyris-register --name <pipeline>`
3. Or trigger pipeline once manually

### Asset triggers not working

1. Check `asset_subscriptions` table in DynamoDB
2. Verify `asset_schedule` in pipeline definition
3. Run `polyris-register --name <pipeline>`

### "Profile not found" error

```
Profile not found: prod
   Check ~/.aws/credentials or ~/.aws/config
```

Verify profile exists:
```bash
aws configure list-profiles
cat ~/.aws/config
```

### "State machine not found" error

1. Check pipeline name matches exactly
2. Try with full ARN instead of name
3. Verify region is correct
