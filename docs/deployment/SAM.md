# AWS SAM Deployment Guide

Polyris uses AWS SAM (Serverless Application Model) to deploy shared infrastructure.

## Prerequisites

1. **AWS SAM CLI** — [Install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
2. **AWS credentials** configured
3. **S3 bucket** for SAM artifacts (auto-managed by SAM)

## First-Time Setup

```bash
# Create state/artifacts bucket (one time)
# Replace <your-namespace> with your own prefix (e.g. your org or project name)
aws s3 mb s3://<your-namespace>-polyris-state

# Configure
cd sam
cp samconfig.toml.example samconfig.toml
# Edit samconfig.toml — set stack_name, Namespace, Stage, SlackWebhookEndpoint, etc.

# Deploy
sam build
sam deploy   # reads stack_name + all parameters from samconfig.toml
```

> **About the stack name in the commands below.** `polyris-dev` is an *example* —
> replace it with your own `stack_name` from `samconfig.toml`. The `sam` commands
> (`deploy`, `delete`) read that file automatically, so the single source of truth
> is `samconfig.toml`. The commands that **don't** read it — `./deploy.sh` (UI) and
> `aws cloudformation describe-stacks` — need the name passed explicitly, so keep it
> identical to `samconfig.toml`.

## Subsequent Deploys

```bash
cd sam
sam build && sam deploy   # stack_name + parameters from samconfig.toml
```

## Parameters

All parameters are in `samconfig.toml`:

| Parameter | Description | Default | Required |
|-----------|-------------|---------|----------|
| `Namespace` | Organization prefix for resource naming | — | yes |
| `Stage` | Deployment stage (dev, prod) | — | yes |
| `SlackWebhookEndpoint` | Slack webhook URL | — | no |
| `DefaultSlackChannel` | Default alert channel | — | no |
| `PagerDutyRoutingKey` | PagerDuty routing key | — | no |
| `EnableCognitoAuth` | Enable auth for Console UI | `false` | no |
| `ConsoleUrlOverride` | Custom domain for Cognito callbacks | — | no |
| `SfnLogLevel` | CloudWatch log level for Standard SFNs | `ERROR` | no |
| `SfnExpressLogLevel` | CloudWatch log level for Express SFNs | `ALL` | no |
| `LogRetentionDays` | Log retention for Standard SFNs and Lambda | `14` | no |
| `ExpressLogRetentionDays` | Log retention for Express SFNs (ALL level = more volume) | `7` | no |

Express SFNs default to `ALL` logging for observability — they execute fast (< 1s) and the
7-day retention keeps costs reasonable.

## SFN Definitions

Step Functions definitions live in `sam/sfn_templates/` as `.tpl.json` files — they are the
**single source of truth**. `template.yaml` references them via `DefinitionUri`:

```
sam/sfn_templates/
  dependency_wrapper/sfn.tpl.json
  helpers/
    run_task/sfn.tpl.json
    failure_handler/sfn.tpl.json
    notify_dependents/sfn.tpl.json
    ...
```

`${var}` placeholders are replaced at deploy time via `DefinitionSubstitutions` in `template.yaml`.

**Editing a SFN definition:**
1. Edit `sam/sfn_templates/*/sfn.tpl.json`
2. `sam build && sam deploy` — SAM inlines the file into `DefinitionString` automatically

**How it works:**
- `sam build` reads each `DefinitionUri` file and embeds it into `.aws-sam/build/template.yaml`
- `sam package` (releases) replaces local paths with S3 URLs for CloudFormation Launch Stack

**SFN types and logging:**
- Standard SFNs (`dependency_wrapper`, `run_task`, `failure_handler`, ...): `SfnLogLevel` (default `ERROR`)
- Express SFNs (`notify_dependents`, `slack_alerter`, `registration`, ...): `SfnExpressLogLevel` (default `ALL`)

## View Outputs

```bash
aws cloudformation describe-stacks \
  --stack-name polyris-dev \
  --query "Stacks[0].Outputs" \
  --output table
```

Key outputs written to SSM automatically:
- `/polyris/{stage}/wrapper_arn`
- `/polyris/{stage}/pipeline_registry_table`
- `/polyris/{stage}/pipeline_tokens_table`
- `/polyris/{stage}/asset_subscriptions_table`

## Destroy

```bash
sam delete --stack-name polyris-dev
```

## Multiple Stages

Deploy to prod by creating a separate `samconfig.prod.toml`:

```bash
sam build
sam deploy --config-file samconfig.prod.toml
```

## Deploying the Console UI

The UI is a static Next.js export served from S3 via CloudFront.

### First time (or after infra changes):

```bash
# 1. Build UI
cd ui && npm ci && npm run build

# 2. Deploy infra
cd ../sam && sam build && sam deploy

# 3. Upload UI — pass your stack name (= stack_name in samconfig.toml) and region.
#    Omitting the stack name makes deploy.sh fall back to a `polyris-dev` default,
#    which fails if you renamed the stack. Add `--profile NAME` for a named profile.
cd ../ui && ./deploy.sh polyris-dev us-east-1 ./out
```

### UI-only updates (no infra changes):

```bash
cd ui && npm run build && ./deploy.sh polyris-dev us-east-1 ./out
```

The script automatically:
- Syncs built files to S3
- Sets correct cache headers (immutable for assets, no-cache for HTML)
- Invalidates CloudFront distribution

### Console URL

After deploy:
```bash
aws cloudformation describe-stacks \
  --stack-name polyris-dev \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleUiUrl'].OutputValue" \
  --output text
```
