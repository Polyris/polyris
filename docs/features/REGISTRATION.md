# Pipeline registration

Most users never need this page: `polyris-deploy` registers a pipeline, every run registers it again, and `polyris-destroy` removes it. Read on if a deployed pipeline is missing from the console, or an asset-scheduled pipeline does not start.

## Overview

A pipeline is registered in two DynamoDB tables of the platform (each named `<namespace>-<stage>-polyris-<table>`):
- **`pipeline-registry`**: the console lists the pipelines it finds here.
- **`asset-subscriptions`**: the pipelines scheduled on an asset (`schedule=[asset]`), which polyris starts when the asset gets a new fact.

Registration happens in three ways:

| Method | When | Automatic |
|--------|------|-----------|
| **`polyris-deploy` / `polyris-destroy`** | Every deploy and destroy | Yes |
| **Pipeline run** | Every run, as its first step | Yes |
| **`polyris-register`** | When you run it | No |

## On deploy and destroy

When you run `polyris-deploy`, it does the following after the CloudFormation stack succeeds:

```
polyris-deploy
    │
    ▼
CloudFormation deploy (creates/updates the pipeline's state machine)
    │
    ▼
pipeline-registry: registered_at = <now>   ← "Deployed at" in the console
    │
    ▼
Start the state machine with register_only=true (does not wait for it)
    │
    ▼
Register_Pipeline state
    ├──► pipeline-registry: state machine ARN, schedule, description, spec version, task count, ...
    └──► asset-subscriptions (if the pipeline is scheduled on assets)
```

`registered_at` is written only by `polyris-deploy`: the console's "Deployed at" shows the last deploy, not the last run.

**On destroy**, `polyris-destroy` deletes the pipeline's CloudFormation stack (its state machine and schedule), stopping the pipeline's running runs until the stack is gone. Then it deletes the pipeline's `pipeline-registry` item and its `asset-subscriptions` items, so the console no longer lists it.

## On every run

Every run starts by registering the pipeline again, so the registry stays current even if its items were deleted by hand:

```
Pipeline start
    │
    ▼
Register_Pipeline state ──► pipeline-registry, asset-subscriptions (if scheduled on assets)
    │
    ▼
register_only?
    ├── true  → end
    └── false → run the tasks
```

A run does not update `registered_at`.

## Manual registration (CLI)

`polyris-register` starts the register-only run without running tasks. It comes with the CLI ([README → Install](../../README.md#install)).

```bash
# Register by ARN
polyris-register arn:aws:states:us-east-1:123456789012:stateMachine:mycompany-dev-polyris-feeds-pipeline

# Register by name (the dag_id)
polyris-register --name feeds-pipeline --region us-east-1
```

### Authentication

It uses the standard AWS credential chain, as the AWS CLI does:

```bash
# Default credentials (env vars, instance profile)
polyris-register --name feeds-pipeline

# AWS profile from ~/.aws/credentials or ~/.aws/config
polyris-register --name feeds-pipeline --profile prod

# Assume an IAM role
polyris-register --name feeds-pipeline --role-arn arn:aws:iam::123456789012:role/deploy

# Cross-account (profile + role)
polyris-register --name feeds-pipeline --profile dev --role-arn arn:aws:iam::210987654321:role/prod
```

### Options

| Option | Short | Description |
|--------|-------|-------------|
| `--name` | `-n` | Pipeline name (alternative to ARN) |
| `--region` | `-r` | AWS region (default: the region of the ARN, or of your AWS profile) |
| `--profile` | `-p` | AWS profile from ~/.aws/credentials or ~/.aws/config |
| `--role-arn` | | IAM role ARN to assume |
| `--namespace` | | Namespace prefix for pipeline search |
| `--json` | | Output result as JSON |

### Example output

```
$ polyris-register --name feeds-pipeline --profile prod

🔍 Looking for pipeline 'feeds-pipeline' in the region of your AWS profile...
   Found: arn:aws:states:us-east-1:123456789012:stateMachine:mycompany-prod-polyris-feeds-pipeline

📝 Registering pipeline...
   Using profile: prod

✅ Registration triggered!
   Execution: arn:aws:states:us-east-1:123456789012:execution:mycompany-prod-polyris-feeds-pipeline:register-mycompany-prod-polyris-feeds-pipeline-1a2b3c4d
   Started: 2026-01-27T13:00:00+00:00

   Pipeline will be registered in:
   • pipeline_registry (for UI discovery)
   • asset_subscriptions (for asset triggers)
```

`polyris-register` does not write `registered_at`: after it, the console's "Deployed at" still shows the last deploy, or nothing if the pipeline was never deployed.

## When to use manual registration

1. **Recovery**: register again after registry items were deleted by hand.
2. **Testing**: check that registration works without running tasks.

## Troubleshooting

### Pipeline not in the console

1. Check the `pipeline-registry` table for an item named after the pipeline.
2. Run `polyris-register --name <pipeline>`, or start a run once.

### Asset-scheduled pipeline does not start

1. Check that the DAG's `schedule=` names the assets.
2. Check the `asset-subscriptions` table for an item per asset and pipeline.
3. Run `polyris-register --name <pipeline>`.

### "ProfileNotFound" error

```
❌ Search failed — ProfileNotFound: The config profile (prod) could not be found
```

Check that the profile exists in `~/.aws/config` or `~/.aws/credentials`: `aws configure list-profiles`.

### "State machine not found" error

1. Check that the name matches the `dag_id` exactly.
2. Try the full ARN instead of the name.
3. Check the region.
