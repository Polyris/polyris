# Pipeline Registration

## Overview

polyris pipelines must be registered in DynamoDB tables for:
- **UI discovery** - Console shows pipelines from `pipeline_registry`
- **Asset triggers** - `notify_asset_consumers` SFN queries `asset_subscriptions`

Registration happens automatically in three ways:

| Method | When | Automatic | Latency |
|--------|------|-----------|---------|
| **polyris-deploy lifecycle** | `polyris-deploy` / `polyris-deploy --destroy` | ✅ Yes | ~2-3 seconds |
| **Pipeline run** | Every execution (self-healing) | ✅ Yes | 0 (inline) |
| **CLI** | Manual `polyris-register` command | ❌ No | ~2-3 seconds |

## polyris-deploy Lifecycle Registration

When you deploy with `polyris-deploy`, the `PipelineRegistration` dynamic resource handles the full lifecycle:

```
polyris-deploy (create)
    │
    ▼
StateMachine created
    │
    ▼
PipelineRegistration.create()
    │
    ▼
StartExecution(register_only=true)
    │
    ├──► pipeline_registry
    ├──► asset_subscriptions
    └──► dag_snapshot
```

**On update** (DAG changed): re-runs registration with new structure. If DAG unchanged (`dag_hash` match), skips entirely.

**On destroy** (`polyris-deploy --destroy`): cleans up DynamoDB directly:
- Deletes `pipeline_registry` entry (removes from UI sidebar)
- Deletes `asset_subscriptions` entries (stops phantom triggers)

```
polyris-deploy --destroy
    │
    ▼
PipelineRegistration.delete()
    │
    ├──► pipeline_registry: DELETE pipeline_name
    └──► asset_subscriptions: DELETE for each asset
    │
    ▼
StateMachine deleted
```

No zombie pipelines, no orphaned subscriptions.

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

This ensures pipelines stay registered even if:
- EventBridge event was missed
- DynamoDB items were deleted
- Subscriptions expired (TTL)

## Manual Registration (CLI)

Use `polyris-register` to register without running tasks:

```bash
# Install
pip install polyris

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

🔍 Looking for pipeline 'feeds-pipeline' in us-east-1...
   Found: arn:aws:states:us-east-1:123:stateMachine:polyris-prod-feeds-pipeline

📝 Registering pipeline...
   Using profile: prod

✅ Registration triggered!
   Execution: arn:aws:states:us-east-1:123:execution:feeds-pipeline:reg-abc123
   Started: 2026-01-27T13:00:00

   Pipeline will be registered in:
   • pipeline_registry (for UI discovery)
   • asset_subscriptions (for asset triggers)
```

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
    - run: polyris-deploy --yes
    
    # Optional: explicit registration (EventBridge handles this too)
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
❌ Profile not found: prod
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
