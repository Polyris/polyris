# AWS SAM Deployment Guide

Polyris uses AWS SAM (Serverless Application Model) to deploy shared infrastructure.

## Prerequisites

1. **AWS SAM CLI** — [Install guide](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)
2. **AWS credentials** configured
3. **S3 bucket** for SAM artifacts (auto-managed by SAM)

## First-Time Setup

```bash
# Configure
cd sam
cp samconfig.toml.example samconfig.toml
# Edit samconfig.toml — set stack_name, Namespace, Stage, etc.

# Deploy
sam build
sam deploy --profile <your-profile>   # reads stack_name + all parameters from samconfig.toml
```

> **Shell variables used in the commands below.** Set them once in your terminal:
> ```bash
> STACK_NAME=polyris-dev     # = stack_name in samconfig.toml
> AWS_REGION=us-east-1       # = AwsRegion in samconfig.toml
> AWS_PROFILE=polyris-dev    # your AWS CLI profile
> ```
> `sam deploy` reads `stack_name` from `samconfig.toml` automatically. `./deploy.sh`
> and `aws cloudformation describe-stacks` need it passed explicitly — keep it
> identical to `samconfig.toml`.

## Subsequent Deploys

```bash
cd sam
sam build && sam deploy --profile <your-profile>   # stack_name + parameters from samconfig.toml
```

## Parameters

All parameters are in `samconfig.toml`:

| Parameter | Description | Default | Required |
|-----------|-------------|---------|----------|
| `Namespace` | Organization prefix for resource naming | — | yes |
| `Stage` | Deployment stage (dev, prod) | — | yes |
| `AwsRegion` | AWS region | — | yes |
| `EnableCognitoAuth` | Enable Cognito auth for Console UI | `false` | no |
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
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" \
  --output table \
  --profile "$AWS_PROFILE"
```

Key outputs (all live on the SAM stack itself; `polyris-deploy` reads them
via `describe_stacks` — no SSM copy is written anymore, see
[CHANGELOG](../../CHANGELOG.md)):
- `DependencyWrapperArn`
- `OrchestrationRoleArn`
- `PipelineRegistryTable`
- `PipelineTokensTable`
- `AssetSubscriptionsTable`
- `ResultsBucket`
- `ConsoleUiBucket`, `ConsoleUiDistributionId`, `ConsoleUiUrl`, `ConsoleApiUrl`
- `CognitoUserPoolId`, `CognitoClientId` (only when `EnableCognitoAuth=true`)

## Destroy

```bash
sam delete --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

## Multiple Stages

Deploy to prod by creating a separate `samconfig.prod.toml`:

```bash
sam build
sam deploy --config-file samconfig.prod.toml --profile <prod-profile>
```

## Deploying the Console UI

The UI is a static Next.js export served from S3 via CloudFront.

### First time (or after infra changes):

```bash
# 1. Build UI
cd ui && npm ci && npm run build

# 2. Deploy infra
cd ../sam && sam build && sam deploy --profile "$AWS_PROFILE"

# 3. Upload UI — pass your stack name (= stack_name in samconfig.toml) and region.
#    Omitting the stack name makes deploy.sh fall back to a `polyris-dev` default,
#    which fails if you renamed the stack.
cd ../ui && ./deploy.sh "$STACK_NAME" "$AWS_REGION" ./out --profile "$AWS_PROFILE"
```

### UI-only updates (no infra changes):

```bash
cd ui && npm run build && ./deploy.sh "$STACK_NAME" "$AWS_REGION" ./out --profile "$AWS_PROFILE"
```

The script automatically:
- Syncs built files to S3
- Sets correct cache headers (immutable for assets, no-cache for HTML)
- Invalidates CloudFront distribution

### Console URL

After deploy:
```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='ConsoleUiUrl'].OutputValue" \
  --output text \
  --profile "$AWS_PROFILE"
```
