# What the polyris platform stack deploys

Reference. Every resource the polyris platform stack (`polyris-up`) creates during install, grouped by purpose. Read this before approving a polyris deployment or when you need to find where a specific piece of runtime state lives (which log group, which DDB table, which state machine).

Companion to [IAM_PERMISSIONS.md](IAM_PERMISSIONS.md) — this doc says *what* runs where; the IAM doc says *who* has permission to touch each piece. Runtime behaviour (data flows, how the dispatcher decides each task, trigger rule evaluation) is in [ARCHITECTURE.md](../architecture/ARCHITECTURE.md).

## Contents

- [The one distinction that matters: the platform vs your workloads](#the-one-distinction-that-matters-the-platform-vs-your-workloads)
- [Inventory by service](#inventory-by-service)
  - [Step Functions state machines (5)](#step-functions-state-machines-5)
  - [Lambda functions (8)](#lambda-functions-8)
  - [EventBridge rule (1)](#eventbridge-rule-1)
  - [DynamoDB tables (8)](#dynamodb-tables-8)
  - [S3 buckets (2)](#s3-buckets-2)
  - [Cognito (1 user pool + 1 client)](#cognito-1-user-pool--1-client)
  - [CloudFront + API Gateway (Console UI)](#cloudfront--api-gateway-console-ui)
  - [CloudWatch log groups (10)](#cloudwatch-log-groups-10)
  - [IAM (12 roles + 4 managed policies)](#iam-12-roles--4-managed-policies)
- [Tags](#tags)
- [Where does each pipeline run's state live?](#where-does-each-pipeline-runs-state-live)
- [Stack outputs to read after deploy](#stack-outputs-to-read-after-deploy)
- [Step Functions log levels](#step-functions-log-levels)
- [What the stack does NOT deploy](#what-the-stack-does-not-deploy)

## The one distinction that matters: the platform vs your workloads

Two categories of AWS resources end up in your account.

**A. The polyris platform.** The fixed set the platform stack creates. Every install has exactly these — 5 state machines, 8 Lambdas, 8 DDB tables, 2 S3 buckets, 1 Cognito pool, 1 CloudFront distribution + HTTP API Gateway for the Console UI, 1 EventBridge rule, plus IAM and logs. Purpose: **orchestrate**. It decides when each task runs, calls an AWS service and records what happened. It does not do your ETL work.

**B. Your workloads.** Whatever your pipelines invoke. Every `@task.lambda_function(function_name="my-etl")` points at a Lambda in your account that YOU created outside polyris. Same for `@task.glue_job`, `@task.ecs_task`, `@task.batch_job`, `@task.athena_query`, `@task.sfn`, `@task.emr_step`. Polyris does not deploy or manage these — see [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).

At runtime, `run_task` (A) invokes your workload (B) via the AWS service API, under the role polyris creates (or the task's `role=` in another account). It collects the workload's result and writes it to polyris DDB with the task's status. The dispatcher, woken by that write, starts the next task.

Practical consequences:

- **Cost:** the platform is lean (SFN transitions ≈ $25/million, Lambda ≈ ms billing). Your workload cost dominates.
- **Logs:** the platform's logs are in `/aws/states/<namespace>-<stage>-polyris-*` and `/aws/lambda/<namespace>-<stage>-polyris-*`. Your workload logs are in whatever log group your Lambda / Glue / ECS emits to.
- **Failures:** a failure of the platform is a polyris bug; a workload failure is your task code.
- **Permissions:** the platform runs under IAM roles polyris creates. Workloads execute under IAM roles YOU created for them (though polyris publishes `PolyrisTaskReadPolicy` + `PolyrisTaskWritePolicy` you can attach for xcom access — see [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam)).

## Inventory by service

The names below leave out the prefix of your install. Every resource is named `<namespace>-<stage>-polyris-…`, after the namespace and stage you gave `polyris-up`: with namespace `acme` and stage `dev`, `polyris-reconcile` below is the function `acme-dev-polyris-reconcile`, and `/aws/lambda/polyris-reconcile` is the log group `/aws/lambda/acme-dev-polyris-reconcile`.

### Step Functions state machines (5)

**Orchestration** — invoked per pipeline run:

| Logical name | Purpose |
|--------------|---------|
| `polyris-dep-run-task-helper` | `run_task`: runs one task's work once the dispatcher starts it. Calls the AWS service (StartJobRun for Glue, RunTask for ECS, …), retries, and records the outcome and its asset facts in one write |
| `polyris-bulk-backfill` | Starts one pipeline execution per date of a date range |

**Demo state machines** — safe targets to point `@task.sfn(arn=...)` at while learning:

| Logical name | Behaviour |
|--------------|-----------|
| `polyris-test-success` | Waits 20s then succeeds |
| `polyris-test-failure` | Waits 20s then fails |
| `polyris-test-quick` | Waits 5s then succeeds |

### Lambda functions (8)

| Logical name | Invoked by | Purpose |
|--------------|-----------|---------|
| `polyris-console-api` | API Gateway | Serves the REST API behind the console: pipelines, runs, tasks, operator actions |
| `polyris-reconcile` | The tasks table's stream; the rule `polyris-execution-end` | The dispatcher: decides each waiting task of a run (trigger rule, `wait_for` assets, pause), starts its `run_task`, and ends the run once every task is final |
| `polyris-asset-fanout` | `asset-events` stream | Wakes the tasks waiting on new asset data and starts the pipelines scheduled on that asset |
| `polyris-sweeper` | EventBridge Scheduler, every 5 minutes | Re-checks every run that is due, so a lost wake-up delays a task instead of losing it; fails a task whose work ended without an outcome; retries failed asset-triggered starts; reports the engine's health to the console |
| `polyris-start-run` | Each pipeline run, as its first step | Writes the run's rows: one for the run and one per task |
| `polyris-notify` | `run_task` | Reserved for alert delivery; no route of this console configures it |
| `polyris-ui-bootstrap` | CustomResource, stacks installed by `polyris-up` | Installs the prebuilt console UI into `ConsoleUiBucket`, writes its `config.js` and invalidates CloudFront |
| `polyris-bucket-cleanup` | CustomResource on stack delete | Empties `ResultsBucket` + `ConsoleUiBucket` so CFN can delete them |

### EventBridge rule (1)

| Logical name | Purpose |
|--------------|---------|
| `polyris-execution-end` | On the account's default event bus: sends `polyris-reconcile` the end of a `run_task` or pipeline execution that ended `ABORTED`, `TIMED_OUT` or `FAILED`, unless the engine stopped it itself. An execution stopped in the AWS console then shows in the console within seconds; a lost event waits for the sweeper's check |

### DynamoDB tables (8)

| Logical name | Contents |
|--------------|----------|
| `polyris-pipeline-tokens` | The runtime store, and the busiest table: one row per run and one per task of each run, the tasks' outputs (`output#{pipeline}#{task}#{date}`), warnings and the engine's own rows. Its stream wakes the dispatcher when a row changes |
| `polyris-pipeline-registry` | One row per deployed pipeline: its state machine, schedule, the spec version it runs (`spec_id`, `task_count`) and the format it was deployed with |
| `polyris-pipeline-specs` | Every task's spec, which `polyris-deploy` writes before the pipeline's stack: each spec text once under `{pipeline}#text` by its sha256, and each version under `{pipeline}#{fingerprint}` naming its tasks' texts. Never changes, never expires. The Console draws a pipeline's graph from it |
| `polyris-dep-subscriptions` | One pointer per waiting task and per `wait_for` asset it waits on. Read by asset-fanout; deleted once the task has left its wait |
| `polyris-task-events` | Append-only audit trail of task lifecycle events (dependency decisions, started, auto-retried, finished, operator decisions). Feeds the Console's per-task History tab |
| `polyris-asset-events` | Append-only asset facts, one per materialisation, with the partition each belongs to (`asset-partition-index` finds them by partition date). Its stream feeds asset-fanout, which wakes `wait_for` tasks and starts asset-scheduled pipelines |
| `polyris-asset-subscriptions` | (asset, downstream_pipeline) edges — the asset-DAG's dependency graph |
| `polyris-api-tokens` | Reserved for API tokens; nothing in this build creates one |

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
| `polyris-console-api` | Serverless HttpApi (API Gateway v2) | Public HTTPS endpoint of the REST API, which the console UI calls |

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

### IAM (12 roles + 4 managed policies)

Roles the stack creates during install, all named `${Namespace}-${Stage}-polyris-*-role`. Full inventory + purpose per role in [IAM_PERMISSIONS.md#iam-roles-polyris-creates](IAM_PERMISSIONS.md#iam-roles-polyris-creates).

Two managed policies the stack publishes for you to attach to YOUR workloads' execution roles: `polyris-task-read`, `polyris-task-write`. Details in [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam). The third, `polyris-reconcile`, holds the dispatcher's own grants; the stack attaches it to its reconcile, asset-fanout and sweeper roles. The fourth, `polyris-deploy-policy`, is for the roles that run `polyris-deploy`.

## Tags

Every resource in the stack that AWS can tag carries three tags. The resources `polyris-deploy` creates for a pipeline (its state machine and log group) carry four:

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
| "Which tasks depend on this one?" | The pipeline's graph in the Console, or `polyris-output --graph` in the pipeline's folder |
| "Why is this task still waiting?" | The task's row in `polyris-pipeline-tokens`: `reasons` says what it waits for, as the dispatcher recorded it. The `/aws/lambda/polyris-reconcile` log group has one line per pass and one per row the pass changed |
| "Is task coordination healthy?" | The **System** pill in the Console header, or `GET /api/health` → `checks.coordination.status` |
| "When did this task last transition to `failed`?" | `polyris-task-events` DDB, query by `task_run_id` |
| "Did Cognito reject the login?" | CloudTrail → filter EventSource `cognito-idp.amazonaws.com` |

## Stack outputs to read after deploy

You need the platform stack's outputs to find the console, add users or point a REST client at the API. In the CloudFormation console, open the stack `<namespace>-<stage>` → **Outputs**, or:

```bash
aws cloudformation describe-stacks --stack-name acme-dev --query "Stacks[0].Outputs" --output table
```

| Output | What it is for |
|---|---|
| `ConsoleUiUrl` | The console's address; `polyris-up` and `polyris-deploy` print it |
| `ConsoleApiUrl` | The REST API's base URL ([API.md](../operations/API.md)) |
| `CognitoUserPoolId`, `CognitoClientId` | Adding console users; the console's sign-in |
| `OrchestrationRoleArn`, `StartRunFunctionArn`, `PipelineSpecsTable`, `PipelineRegistryTable`, `PipelineTokensTable`, `AssetSubscriptionsTable`, `ResultsBucket`, `PipelineScheduleRoleArn` | Read by `polyris-deploy`; `PipelineScheduleRoleArn` is also exported, and every scheduled pipeline's stack imports it |
| `PipelineDeployPolicyArn` | The managed policy to attach to the roles that run `polyris-deploy` ([IAM_PERMISSIONS.md](IAM_PERMISSIONS.md#the-deploy-policy)) |
| `TestQuickSfnArn`, `TestFailureSfnArn` | The demo state machines; `polyris-init --hello` reads `TestQuickSfnArn` |
| `PolyrisTaskReadPolicyArn`, `PolyrisTaskWritePolicyArn` | The managed policies to attach to your task roles for xcom |
| `ConsoleUiBucket`, `ConsoleUiDistributionId` | The UI upload of a from-source deploy ([FROM_SOURCE.md](FROM_SOURCE.md)) |

## Step Functions log levels

| Parameter | Applies to | Default | Why |
|---|---|---|---|
| `SfnLogLevel` | `run_task` and `bulk_backfill` | `ERROR` | These run for every task of every run — full logging is expensive |
| `SfnExpressLogLevel` | The three demo state machines (`test-*`) | `ALL` | They run in seconds; full logging is cheap and useful |

Change one with `polyris-up --param SfnLogLevel=ALL`. The definitions themselves live in `sam/sfn_templates/`; to change one, deploy from source ([FROM_SOURCE.md](FROM_SOURCE.md)).

## What the stack does NOT deploy

Explicit non-scope, in case you're auditing:

- **Your data plane.** No S3 buckets for your raw / bronze / silver / gold tables. No Glue databases (except a shared workgroup in the `examples/testing-infra/` companion stack, not the main stack).
- **Your task compute.** No Lambda functions, Glue jobs, ECS clusters, Batch queues, EMR clusters for your workloads. See [DSL.md](../features/DSL.md#how-polyris-relates-to-aws).
- **Schedules for your pipelines.** Each pipeline's `polyris-deploy` creates its EventBridge Scheduler schedule in its own per-pipeline CFN stack, not in the main polyris stack. Every schedule starts its pipeline with the platform's schedule role, `polyris-schedule-role`, which the pipeline's stack imports from the export `<namespace>-<stage>-polyris-schedule-role-arn`.
- **VPCs, subnets, security groups.** Polyris runs Lambdas without VPC attachment. If your Glue/ECS/Batch tasks need VPC access, that's your infra — see the AWS docs for those services.
- **KMS keys.** All polyris data uses the default encryption of DynamoDB and S3, with keys AWS owns. No template parameter sets a customer-managed KMS key: for one, add an encryption config to the DDB tables and S3 buckets in `sam/template.yaml` and deploy from source ([FROM_SOURCE.md](FROM_SOURCE.md)).
