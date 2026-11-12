# What the polyris SAM stack deploys

Reference. Every resource the polyris CloudFormation stack creates during install, grouped by purpose. Read this before approving a polyris deployment or when you need to find where a specific piece of runtime state lives (which log group, which DDB table, which state machine).

Companion to [IAM_PERMISSIONS.md](IAM_PERMISSIONS.md) — this doc says *what* runs where; the IAM doc says *who* has permission to touch each piece. Runtime behaviour (data flows, dependency wrapper mechanics, trigger rule evaluation) is in [ARCHITECTURE.md](../architecture/ARCHITECTURE.md).

## Contents

- [The one distinction that matters: wrappers vs workloads](#the-one-distinction-that-matters-wrappers-vs-workloads)
- [Inventory by service](#inventory-by-service)
  - [Step Functions state machines (13)](#step-functions-state-machines-13)
  - [Lambda functions (8)](#lambda-functions-8)
  - [DynamoDB tables (7)](#dynamodb-tables-7)
  - [S3 buckets (2)](#s3-buckets-2)
  - [Cognito (1 user pool + 1 client)](#cognito-1-user-pool--1-client)
  - [CloudFront + API Gateway (Console UI)](#cloudfront--api-gateway-console-ui)
  - [CloudWatch log groups (17)](#cloudwatch-log-groups-17)
  - [IAM (11 roles + 3 managed policies)](#iam-11-roles--3-managed-policies)
- [Where does each pipeline run's state live?](#where-does-each-pipeline-runs-state-live)
- [Stack outputs to read after deploy](#stack-outputs-to-read-after-deploy)
- [Step Functions definition sources](#step-functions-definition-sources)
- [What the stack does NOT deploy](#what-the-stack-does-not-deploy)

## The one distinction that matters: wrappers vs workloads

Two categories of AWS resources end up in your account.

**A. Polyris wrappers.** The fixed set the SAM stack creates. Every install has exactly these — 13 state machines, 8 Lambdas, 7 DDB tables, 2 S3 buckets, 1 Cognito pool, 1 CloudFront distribution + HTTP API Gateway for the Console UI, plus IAM and logs. Purpose: **orchestrate**. They wait for dependencies, decide when to run something, call an AWS service, record what happened, and notify downstream. They do not do your ETL work.

**B. Your workloads.** Whatever your pipelines invoke. Every `@task.lambda_function(function_name="my-etl")` points at a Lambda in your account that YOU created outside polyris. Same for `@task.glue_job`, `@task.ecs_task`, `@task.batch_job`, `@task.athena_query`, `@task.sfn`, `@task.emr_step`. Polyris does not deploy or manage these — see [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).

At runtime the flow is: polyris wrapper (A) → assumes the polyris task role → invokes your workload (B) via the AWS service API → collects the workload's result → writes it to polyris DDB → notifies the next wrapper.

Practical consequences:

- **Cost:** polyris wrappers are lean (SFN transitions ≈ $25/million, Lambda ≈ ms billing). Your workload cost dominates.
- **Logs:** wrapper logs are in `/aws/states/polyris-*` and `/aws/lambda/polyris-*`. Your workload logs are in whatever log group your Lambda / Glue / ECS emits to.
- **Failures:** a wrapper failure is a polyris bug; a workload failure is your task code.
- **Permissions:** wrappers execute under IAM roles polyris creates. Workloads execute under IAM roles YOU created for them (though polyris publishes `PolyrisTaskReadPolicy` + `PolyrisTaskWritePolicy` you can attach for xcom access — see [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam)).

## Inventory by service

Every resource name below is prefixed with `${Namespace}-${Stage}-polyris-` — the two CFN parameters set at install time. Default: `polyris-dev-polyris-`. Examples below use the default; substitute your values.

### Step Functions state machines (13)

**Orchestration workhorses** — invoked per pipeline run:

| Logical name | Purpose |
|--------------|---------|
| `polyris-dependency-wrapper` | One execution per task in a pipeline. Waits for dependencies via `waitForTaskToken`, invokes `run-task-helper` when unblocked, records final status |
| `polyris-run-task-helper` | Called by the wrapper. Routes to the right AWS-service integration (StartJobRun for Glue, RunTask for ECS, etc.), normalizes the result, handles retries and `xcom.push()` markers |
| `polyris-notify-dependents` | Fires when a task completes. Queries `dep-subscriptions` DDB table, sends a `SendTaskSuccess`/`SendTaskFailure` token to each subscribed downstream wrapper |
| `polyris-bulk-backfill` | Drives the backfill queue — walks the date range, spawns one pipeline execution per date |

**Support helpers** — invoked when specific things happen:

| Logical name | Triggered by |
|--------------|--------------|
| `polyris-failure-handler` | Any task failure that needs alerting / marker writes |
| `polyris-pause-waiter` | A pipeline paused via Console; blocks until resumed |
| `polyris-restart-task-helper` | Manual "Restart task" action from Console |
| `polyris-restart-wrapper` | Manual "Restart pipeline" action from Console |
| `polyris-registration-helper` | Triggered by `polyris-deploy` after CFN deploy; registers pipeline deps and asset subscriptions |
| `polyris-register-pipeline` | Deployed but not called by any current code path — superseded by the inline `Register_Pipeline` state in each pipeline's generated SFN |

**Demo state machines** — safe targets to point `@task.sfn(arn=...)` at while learning:

| Logical name | Behaviour |
|--------------|-----------|
| `polyris-test-success` | Succeeds immediately |
| `polyris-test-failure` | Fails immediately |
| `polyris-test-quick` | Waits 1s then succeeds |

### Lambda functions (8)

| Logical name | Invoked by | Purpose |
|--------------|-----------|---------|
| `polyris-console-api` | API Gateway | Serves the REST endpoints behind the Console UI (list pipelines, describe run, trigger execution, manual action, etc.) |
| `polyris-query-subscriptions` | `notify-dependents` SFN | Reads `dep-subscriptions` to find who to notify when an upstream completes |
| `polyris-reconcile` | `registration` and `notify-dependents` SFNs, asset-fanout | Decides whether a waiting task is ready (trigger rule, `wait_for` assets, pause) and signals its wrapper (ADR #130) |
| `polyris-asset-fanout` | `asset-events` stream | Wakes the tasks waiting on a new asset fact and starts the pipelines scheduled on that asset |
| `polyris-sweeper` | EventBridge Scheduler, every 5 minutes | Re-checks the waiting tasks whose due time has come, so a lost wake-up hint delays a task instead of losing it; aborts a task whose wrapper is gone (ADR #130 §8) |
| `polyris-notify` | `failure-handler` SFN | Publishes alerts through the configured notification channels |
| `polyris-ui-bootstrap` | CustomResource, stacks installed by `polyris-up` | Copies the prebuilt Console UI from the artifacts bucket into `ConsoleUiBucket`, deletes files of the previous build, writes `config.js` from the stack's values, invalidates CloudFront. Absent on a from-source `sam deploy` (there `ui/deploy.sh` uploads the UI) |
| `polyris-bucket-cleanup` | CustomResource on stack delete | Empties `ResultsBucket` + `ConsoleUiBucket` so CFN can delete them |

### DynamoDB tables (7)

| Logical name | Contents |
|--------------|----------|
| `polyris-pipeline-tokens` | The runtime store. One row per task execution — status, timings, xcom output, `waitForTaskToken` tokens. Also one row per `output#{pipeline}#{task}#{date}` for xcom canonical output. Hottest table |
| `polyris-pipeline-registry` | One row per deployed pipeline. Holds the DAG snapshot the Console renders |
| `polyris-dep-subscriptions` | One pointer per waiting task and per thing it waits on (an upstream task of its run, or a `wait_for` asset). Read by `query-subscriptions` and asset-fanout; deleted once the task is signalled |
| `polyris-task-events` | Append-only audit trail of task lifecycle events (queued, started, succeeded, failed, restarted, manually-resolved). Feeds the Console's per-task History tab |
| `polyris-asset-events` | Append-only asset facts, one per materialisation. Its stream feeds asset-fanout, which wakes `wait_for` tasks and starts asset-scheduled pipelines |
| `polyris-asset-subscriptions` | (asset, downstream_pipeline) edges — the asset-DAG's dependency graph |
| `polyris-api-tokens` | Personal access tokens for the Console REST API (alternative to Cognito login) |

All tables are on-demand billing, so idle cost is $0. Point-in-time recovery is enabled by default on the operational tables.

### S3 buckets (2)

| Logical name | Contents |
|--------------|----------|
| `polyris-console-ui` | The compiled Console UI (HTML + JS + CSS). Read-only, fronted by CloudFront |
| `polyris-results` | Large XCom payloads offloaded via `_s3_ref` claim-check (see [DATA_PASSING.md#large-outputs--claim-check-pattern](../features/DATA_PASSING.md#large-outputs--claim-check-pattern)) |

Both buckets have `BlockPublicAccess` on and are readable only through the CloudFront OAC (UI bucket) or the polyris task/orchestration roles (results bucket).

`polyris-up` also keeps one bucket **outside** the stack: `polyris-artifacts-<account>-<region>`, private and encrypted. It holds the Lambda zips, Step Functions definitions, UI and template of each installed release (keys carry the version and a content hash), because CloudFormation needs that code before it creates the functions. One artifacts bucket serves every platform stack in the region; `polyris-down` deletes it with the last one.

### Cognito (1 user pool + 1 client)

| Logical name | Purpose |
|--------------|---------|
| `polyris-user-pool` | Authenticates Console users. Emits ID tokens the Console API validates via JWKS |
| `polyris-user-pool-client` | The OAuth2 client the Console UI uses to exchange login credentials for tokens |

When the `AdminEmail` parameter is set (`polyris-up --admin-email`), the stack also creates that user (`AWS::Cognito::UserPoolUser`); Cognito emails it a temporary password. Changing `AdminEmail` replaces the user.

MFA is available but off by default; enable per your org's policy via `aws cognito-idp update-user-pool`.

### CloudFront + API Gateway (Console UI)

| Logical name | Type | Purpose |
|--------------|------|---------|
| `polyris-console-ui-distribution` | CloudFront distribution | Serves the Console UI over HTTPS with an AWS-managed cert |
| `polyris-console-ui-oac` | CloudFront OriginAccessControl | Lets CloudFront read from `polyris-console-ui` bucket without making the bucket public |
| `polyris-console-ui-headers` | CloudFront ResponseHeadersPolicy | Sets security headers (CSP, HSTS, X-Frame-Options) |
| `polyris-console-ui-url-rewrite` | CloudFront Function | Client-side routing — rewrites SPA paths to `/index.html` |
| `polyris-console-api` | Serverless HttpApi (API Gateway v2) | Public HTTPS endpoint the Console UI + PAT clients call |

### CloudWatch log groups (17)

One per state machine (the test machines share one) and one per Lambda, except bucket-cleanup. Retention: 30 days by default (change via the `LogRetentionDays` CFN parameter at install).

| Category | Log group name pattern |
|----------|------------------------|
| Wrapper SFNs (7) | `/aws/states/polyris-dependency-wrapper`, `-run-task-helper`, `-failure-handler`, `-notify-dependents`, `-pause-waiter`, `-restart-wrapper`, `-restart-task-helper` |
| Support SFNs (2) | `/aws/states/polyris-registration-helper`, `-bulk-backfill` |
| Demo SFNs (1) | `/aws/states/polyris-test` (shared) |
| Console API (1) | `/aws/lambda/polyris-console-api` |
| Ops Lambdas (5) | `/aws/lambda/polyris-notify`, `-query-subscriptions`, `-reconcile`, `-asset-fanout`, `-sweeper` |
| UI bootstrap (1, stacks installed by `polyris-up`) | `/aws/lambda/polyris-ui-bootstrap` |

Your workload logs (Lambdas / Glue jobs / ECS tasks that your pipelines invoke) are NOT in this list — they go to wherever YOU configured them.

### IAM (11 roles + 3 managed policies)

Roles the stack creates during install, all named `${Namespace}-${Stage}-polyris-*-role`. Full inventory + purpose per role in [IAM_PERMISSIONS.md#iam-roles-polyris-creates](IAM_PERMISSIONS.md#iam-roles-polyris-creates).

Two managed policies the stack publishes for you to attach to YOUR workloads' execution roles: `polyris-task-read`, `polyris-task-write`. Details in [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam). The third, `polyris-reconcile`, holds the reconciler's own grants; the stack attaches it to its reconcile, asset-fanout and sweeper roles.

## Where does each pipeline run's state live?

A common admin/developer question during an incident. Table maps concepts to concrete resources.

| Question | Look here |
|----------|-----------|
| "Which task is stuck?" | Console UI → pipeline → run, or `polyris-pipeline-tokens` DDB table (partition key = execution name) |
| "Why did the wrapper fail?" | `/aws/states/polyris-dependency-wrapper` log group, filter by execution ARN |
| "What's the current DAG for this pipeline?" | `polyris-pipeline-registry` DDB (partition key = pipeline name) |
| "What did the upstream task return?" | `polyris-pipeline-tokens` DDB, key = `output#{pipeline}#{task}#{date}`, field `result` — or the Console UI's Task Detail → Output tab |
| "Where's the large XCom payload the task pushed?" | `polyris-results` S3 bucket, at the `_s3_ref` path the DDB `result` field points to |
| "Why did the Console API return 500?" | `/aws/lambda/polyris-console-api` log group, filter by request ID from the browser response |
| "Which tasks depend on this one?" | `polyris-dep-subscriptions` DDB (GSI: `subscriber-index`) |
| "When did this task last transition to `failed`?" | `polyris-task-events` DDB, query by `task_run_id` |
| "Did Cognito reject the login?" | CloudTrail → filter EventSource `cognito-idp.amazonaws.com` |

## Stack outputs to read after deploy

`polyris-deploy` reads these directly from the SAM stack via
`describe_stacks` (no SSM copy). You need them when integrating with the
stack from outside — CI hooks, cross-account role assumers, or manual
troubleshooting.

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" \
  --output table \
  --profile "$AWS_PROFILE"
```

Key outputs:

| Output | Consumers |
|---|---|
| `DependencyWrapperArn` | polyris-deploy — root SFN each pipeline invokes |
| `OrchestrationRoleArn` | polyris-deploy — role attached to per-pipeline state machines |
| `PipelineRegistryTable` | Console API, notify Lambda |
| `PipelineTokensTable` | Every wrapper execution, Console API, notify Lambda |
| `AssetSubscriptionsTable` | polyris-deploy |
| `ResultsBucket` | xcom S3 spill, Console API sign-URL flow |
| `ConsoleUiBucket`, `ConsoleUiDistributionId`, `ConsoleUiUrl` | UI `deploy.sh` |
| `ConsoleApiUrl` | UI runtime config |
| `CognitoUserPoolId`, `CognitoClientId` | UI auth, CLI PAT flow (only when `EnableCognitoAuth=true`) |

## Step Functions definition sources

State machine definitions live in `sam/sfn_templates/` as `.tpl.json`
files — the **single source of truth**. `template.yaml` references them via
`DefinitionUri`:

```
sam/sfn_templates/
  dependency_wrapper/sfn.tpl.json
  helpers/
    run_task/sfn.tpl.json
    failure_handler/sfn.tpl.json
    notify_dependents/sfn.tpl.json
    ...
```

`${var}` placeholders get replaced at deploy time via
`DefinitionSubstitutions` in `template.yaml`.

**Editing a definition:**
1. Edit `sam/sfn_templates/*/sfn.tpl.json`.
2. `sam build && sam deploy` — SAM inlines the file into `DefinitionString`
   automatically.

**Logging levels** are separate for the two SFN types (both configurable
in `samconfig.toml`):

| Parameter | Applies to | Default | Why |
|---|---|---|---|
| `SfnLogLevel` | Standard SFNs (`dependency_wrapper`, `run_task`, `failure_handler`, …) | `ERROR` | These run for the pipeline's lifetime — full logging is expensive |
| `SfnExpressLogLevel` | Express SFNs (`notify_dependents`, `registration`, …) | `ALL` | Express runs are sub-second; full logging is cheap and useful |

## What the stack does NOT deploy

Explicit non-scope, in case you're auditing:

- **Your data plane.** No S3 buckets for your raw / bronze / silver / gold tables. No Glue databases (except a shared workgroup in the `examples/testing-infra/` companion stack, not the main stack).
- **Your task compute.** No Lambda functions, Glue jobs, ECS clusters, Batch queues, EMR clusters for your workloads. See [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).
- **EventBridge rules for your pipeline schedules.** Each pipeline's `polyris-deploy` creates the EventBridge rule in its own per-pipeline CFN stack, not in the main polyris stack.
- **VPCs, subnets, security groups.** Polyris runs Lambdas without VPC attachment. If your Glue/ECS/Batch tasks need VPC access, that's your infra — see the AWS docs for those services.
- **KMS keys.** All polyris data uses AWS-managed keys by default. To use a customer-managed KMS key, override the encryption config on the DDB tables and S3 buckets via CFN parameter (not enabled by default — see `sam/template.yaml`).
