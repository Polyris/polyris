# What the polyris SAM stack deploys

Reference. Every resource the polyris CloudFormation stack creates during install, grouped by purpose. Read this before approving a polyris deployment or when you need to find where a specific piece of runtime state lives (which log group, which DDB table, which state machine).

Companion to [IAM_PERMISSIONS.md](IAM_PERMISSIONS.md) — this doc says *what* runs where; the IAM doc says *who* has permission to touch each piece. Runtime behaviour (data flows, how the dispatcher decides each task, trigger rule evaluation) is in [ARCHITECTURE.md](../architecture/ARCHITECTURE.md).

## Contents

- [The one distinction that matters: the platform vs your workloads](#the-one-distinction-that-matters-the-platform-vs-your-workloads)
- [Inventory by service](#inventory-by-service)
  - [Step Functions state machines (5)](#step-functions-state-machines-5)
  - [Lambda functions (8)](#lambda-functions-8)
  - [DynamoDB tables (8)](#dynamodb-tables-8)
  - [S3 buckets (2)](#s3-buckets-2)
  - [Cognito (1 user pool + 1 client)](#cognito-1-user-pool--1-client)
  - [CloudFront + API Gateway (Console UI)](#cloudfront--api-gateway-console-ui)
  - [CloudWatch log groups (10)](#cloudwatch-log-groups-10)
  - [IAM (11 roles + 3 managed policies)](#iam-11-roles--3-managed-policies)
- [Tags](#tags)
- [Where does each pipeline run's state live?](#where-does-each-pipeline-runs-state-live)
- [Stack outputs to read after deploy](#stack-outputs-to-read-after-deploy)
- [Step Functions definition sources](#step-functions-definition-sources)
- [What the stack does NOT deploy](#what-the-stack-does-not-deploy)

## The one distinction that matters: the platform vs your workloads

Two categories of AWS resources end up in your account.

**A. The polyris platform.** The fixed set the SAM stack creates. Every install has exactly these — 5 state machines, 8 Lambdas, 8 DDB tables, 2 S3 buckets, 1 Cognito pool, 1 CloudFront distribution + HTTP API Gateway for the Console UI, plus IAM and logs. Purpose: **orchestrate**. It decides when each task runs, calls an AWS service and records what happened. It does not do your ETL work.

**B. Your workloads.** Whatever your pipelines invoke. Every `@task.lambda_function(function_name="my-etl")` points at a Lambda in your account that YOU created outside polyris. Same for `@task.glue_job`, `@task.ecs_task`, `@task.batch_job`, `@task.athena_query`, `@task.sfn`, `@task.emr_step`. Polyris does not deploy or manage these — see [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).

At runtime, `run_task` (A) invokes your workload (B) via the AWS service API, under the role polyris creates (or the task's `role=` in another account). It collects the workload's result and writes it to polyris DDB with the task's status. The dispatcher, woken by that write, starts the next task.

Practical consequences:

- **Cost:** the platform is lean (SFN transitions ≈ $25/million, Lambda ≈ ms billing). Your workload cost dominates.
- **Logs:** the platform's logs are in `/aws/states/polyris-*` and `/aws/lambda/polyris-*`. Your workload logs are in whatever log group your Lambda / Glue / ECS emits to.
- **Failures:** a failure of the platform is a polyris bug; a workload failure is your task code.
- **Permissions:** the platform runs under IAM roles polyris creates. Workloads execute under IAM roles YOU created for them (though polyris publishes `PolyrisTaskReadPolicy` + `PolyrisTaskWritePolicy` you can attach for xcom access — see [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam)).

## Inventory by service

Every resource name below is prefixed with `${Namespace}-${Stage}-polyris-` — the two CFN parameters set at install time. Default: `polyris-dev-polyris-`. Examples below use the default; substitute your values.

### Step Functions state machines (5)

**Orchestration** — invoked per pipeline run:

| Logical name | Purpose |
|--------------|---------|
| `polyris-run-task-helper` | `run_task`: started by the dispatcher for one task once it may run (ADR #137 §1). Routes to the right AWS-service integration (StartJobRun for Glue, RunTask for ECS, etc.), retries, and records the outcome and its asset facts in one write |
| `polyris-bulk-backfill` | Starts one pipeline execution per date of a date range |

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
| `polyris-reconcile` | The tasks table's stream | The dispatcher (ADR #137): passes over a run's rows, decides each waiting row (trigger rule, `wait_for` assets, pause), starts its `run_task`, and answers the run once every row is final |
| `polyris-asset-fanout` | `asset-events` stream | Wakes the tasks waiting on a new asset fact and starts the pipelines scheduled on that asset |
| `polyris-sweeper` | EventBridge Scheduler, every 5 minutes | Makes one full pass of the dispatcher over each run that is due, so a lost wake-up hint delays a task instead of losing it (ADR #137 §7). Its passes alone check executions: a task whose `run_task` ended without an outcome fails, and the engine stops a run whose execution ended before its answer or that never became active. Stops the rows of a run whose run row is missing. Retries failed push starts, hourly replays the pushes of recent facts, and reports the engine's health, both streams and their mappings included (ADR #130 §8, ADR #137 §6) |
| `polyris-start-run` | Each pipeline run, after its registration (`lambda:invoke.waitForTaskToken`) | Reads the run's task specs, writes the run row with the run's task token, the run's input and one row per task, then marks the run active (ADR #137 §1) |
| `polyris-notify` | `run_task` | Publishes alerts through the configured notification channels |
| `polyris-ui-bootstrap` | CustomResource, stacks installed by `polyris-up` | Copies the prebuilt Console UI from the artifacts bucket into `ConsoleUiBucket`, deletes files of the previous build, writes `config.js` from the stack's values, invalidates CloudFront. Absent on a from-source `sam deploy` (there `ui/deploy.sh` uploads the UI) |
| `polyris-bucket-cleanup` | CustomResource on stack delete | Empties `ResultsBucket` + `ConsoleUiBucket` so CFN can delete them |

### DynamoDB tables (8)

| Logical name | Contents |
|--------------|----------|
| `polyris-pipeline-tokens` | The runtime store. One row per task execution — status, timings, xcom output, `waitForTaskToken` tokens. Also one row per `output#{pipeline}#{task}#{date}` for xcom canonical output, and the coordination rows the sweeper keeps (`_engine_health`, `_sweeper_lease`, warnings). Its sparse `attention-index` lists the rows that need attention by due time: waiting tasks, tasks held in their pause wait, push retries, warnings. Its sparse `run-start-index` lists the run rows by the day each run started, newest first, for the console's run lists (ADR #137 §5). Its stream, with both images, wakes the dispatcher when a row changes (ADR #137 §7). Hottest table |
| `polyris-pipeline-registry` | One row per deployed pipeline: its state machine, schedule, the spec version it runs (`spec_id`, `task_count`) and the format it was deployed with |
| `polyris-pipeline-specs` | Every task's spec, which `polyris-deploy` writes before the pipeline's stack: each spec text once under `{pipeline}#text` by its sha256, and each version under `{pipeline}#{fingerprint}` naming its tasks' texts. Never changes, never expires. The Console draws a pipeline's graph from it |
| `polyris-dep-subscriptions` | One pointer per waiting task and per `wait_for` asset it waits on. Read by asset-fanout; deleted once the task has left its wait |
| `polyris-task-events` | Append-only audit trail of task lifecycle events (queued, started, succeeded, failed, restarted, manually-resolved). Feeds the Console's per-task History tab |
| `polyris-asset-events` | Append-only asset facts, one per materialisation, with the partition each belongs to (`asset-partition-index` finds them by partition date). Its stream feeds asset-fanout, which wakes `wait_for` tasks and starts asset-scheduled pipelines |
| `polyris-asset-subscriptions` | (asset, downstream_pipeline) edges — the asset-DAG's dependency graph |
| `polyris-api-tokens` | Personal access tokens for the Console REST API (alternative to Cognito login) |

All tables are on-demand billing, so idle cost is $0. Point-in-time recovery is enabled by default on the operational tables; `pipeline-specs` has none, since a redeploy writes its items again.

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

### CloudWatch log groups (10)

One per state machine (the test machines share one) and one per Lambda, except bucket-cleanup. Retention: 30 days by default (change via the `LogRetentionDays` CFN parameter at install).

| Category | Log group name pattern |
|----------|------------------------|
| Orchestration SFNs (2) | `/aws/states/polyris-run-task-helper`, `-bulk-backfill` |
| Demo SFNs (1) | `/aws/states/polyris-test` (shared) |
| Console API (1) | `/aws/lambda/polyris-console-api` |
| Ops Lambdas (5) | `/aws/lambda/polyris-notify`, `-reconcile`, `-asset-fanout`, `-sweeper`, `-start-run` |
| UI bootstrap (1, stacks installed by `polyris-up`) | `/aws/lambda/polyris-ui-bootstrap` |

Your workload logs (Lambdas / Glue jobs / ECS tasks that your pipelines invoke) are NOT in this list — they go to wherever YOU configured them.

### IAM (11 roles + 3 managed policies)

Roles the stack creates during install, all named `${Namespace}-${Stage}-polyris-*-role`. Full inventory + purpose per role in [IAM_PERMISSIONS.md#iam-roles-polyris-creates](IAM_PERMISSIONS.md#iam-roles-polyris-creates).

Two managed policies the stack publishes for you to attach to YOUR workloads' execution roles: `polyris-task-read`, `polyris-task-write`. Details in [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam). The third, `polyris-reconcile`, holds the reconciler's own grants; the stack attaches it to its reconcile, asset-fanout and sweeper roles.

## Tags

Every resource in the stack that AWS can tag carries three tags. The resources `polyris-deploy` creates for a pipeline (its state machine, log group and scheduler role) carry four (ADR #132):

| Key | Value |
|---|---|
| `polyris:namespace` | the stack's `Namespace`; a pipeline's, from the config it is deployed with |
| `polyris:stage` | the stack's `Stage`; a pipeline's, from the config it is deployed with |
| `polyris:component` | `platform` on this stack's resources, `pipeline` on a pipeline's |
| `polyris:pipeline` | the pipeline's id; pipeline resources only |

Resources AWS cannot tag, such as the sweeper's schedule and the IAM policies, carry none. A stack installed with `polyris-up` also has the stack tag `polyris:platform=true`, which CloudFormation copies to its resources. The artifacts bucket `polyris-up` creates outside the stacks, one per account and region, carries only `polyris:platform=artifacts`. To split your bill by these tags, see [Track costs](../how-to/manage-the-platform.md#track-costs).

## Where does each pipeline run's state live?

A common admin/developer question during an incident. Table maps concepts to concrete resources.

| Question | Look here |
|----------|-----------|
| "Which task is stuck?" | Console UI → pipeline → run, or `polyris-pipeline-tokens` DDB table (partition key = execution name) |
| "Why did the task fail?" | The Console's task detail; then `/aws/states/polyris-run-task-helper` log group, filter by the task's `run_task` execution ARN |
| "What's the current DAG for this pipeline?" | `polyris-pipeline-registry` DDB (partition key = pipeline name) names its `spec_id`; `polyris-pipeline-specs` holds the specs (partition key = `spec_id`), or run `polyris-output --json` |
| "What did the upstream task return?" | `polyris-pipeline-tokens` DDB, key = `output#{pipeline}#{task}#{date}`, field `result` — or the Console UI's Task Detail → Output tab |
| "Where's the large XCom payload the task pushed?" | `polyris-results` S3 bucket, at the `_s3_ref` path the DDB `result` field points to |
| "Why did the Console API return 500?" | `/aws/lambda/polyris-console-api` log group, filter by request ID from the browser response |
| "Which tasks depend on this one?" | `polyris-dep-subscriptions` DDB (GSI: `subscriber-index`) |
| "Why is this task still waiting?" | The task's row in `polyris-pipeline-tokens`: `reasons` says what it waits for, as the dispatcher recorded it. The `/aws/lambda/polyris-reconcile` log group has one line per pass and one per row the pass changed |
| "Is task coordination healthy?" | The **System** pill in the Console header, or `GET /api/health` → `checks.coordination.status` |
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
| `OrchestrationRoleArn` | polyris-deploy — role attached to per-pipeline state machines |
| `PipelineRegistryTable` | Console API, notify Lambda |
| `PipelineTokensTable` | polyris-deploy — the table each pipeline's state machine writes its run's marks and warnings to; Console API, notify Lambda |
| `AssetSubscriptionsTable` | polyris-deploy |
| `PipelineSpecsTable` | polyris-deploy — writes every task's spec before the pipeline's stack |
| `StartRunFunctionArn` | polyris-deploy — the function each run invokes first |
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
  bulk_backfill/sfn.tpl.json
  helpers/
    run_task/sfn.tpl.json
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
| `SfnLogLevel` | `run_task` and `bulk_backfill` | `ERROR` | These run for every task of every run — full logging is expensive |
| `SfnExpressLogLevel` | The three demo state machines (`test-*`) | `ALL` | They run in a second; full logging is cheap and useful |

## What the stack does NOT deploy

Explicit non-scope, in case you're auditing:

- **Your data plane.** No S3 buckets for your raw / bronze / silver / gold tables. No Glue databases (except a shared workgroup in the `examples/testing-infra/` companion stack, not the main stack).
- **Your task compute.** No Lambda functions, Glue jobs, ECS clusters, Batch queues, EMR clusters for your workloads. See [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).
- **EventBridge rules for your pipeline schedules.** Each pipeline's `polyris-deploy` creates the EventBridge rule in its own per-pipeline CFN stack, not in the main polyris stack.
- **VPCs, subnets, security groups.** Polyris runs Lambdas without VPC attachment. If your Glue/ECS/Batch tasks need VPC access, that's your infra — see the AWS docs for those services.
- **KMS keys.** All polyris data uses AWS-managed keys by default. To use a customer-managed KMS key, override the encryption config on the DDB tables and S3 buckets via CFN parameter (not enabled by default — see `sam/template.yaml`).
