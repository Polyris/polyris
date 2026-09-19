# IAM permissions for the polyris lifecycle

Reference for AWS administrators who need to approve a polyris installation and grant the right permissions to the humans and CI roles that install, deploy, operate, and remove it.

Distinct from the **runtime task role** (`${Namespace}-${Stage}-polyris-default-task-role`) that pipelines assume to talk to Glue, Athena, ECS, Batch, and S3 — that role is created by polyris itself during install and documented in [DATA_PASSING.md](../features/DATA_PASSING.md#iam). This doc covers the *operator* permissions to run the polyris tooling, not the *task* permissions polyris configures on your behalf.

## Contents

- [Lifecycle overview](#lifecycle-overview)
- [Install — first-time `sam deploy`](#install--first-time-sam-deploy)
- [Deploy — per-pipeline `polyris-deploy`](#deploy--per-pipeline-polyris-deploy)
- [Operate](#operate)
- [Remove — `sam delete` / stack teardown](#remove--sam-delete--stack-teardown)
- [IAM roles polyris creates](#iam-roles-polyris-creates)
- [Cross-account model](#cross-account-model)

## Lifecycle overview

| Phase | Who runs it | Frequency | AWS-level permissions needed |
|-------|-------------|-----------|------------------------------|
| **Install** | Platform admin or CI role | Once per account | Broad — 118 resources across 10 services |
| **Deploy** | Developer or CI role | Per pipeline, per change | Narrow — CloudFormation + Step Functions + PassRole |
| **Operate** | Console user (Cognito) / on-call engineer | Continuous | None (Console user) or read-only (on-call) |
| **Remove** | Platform admin | Rare | Same as Install + S3 bucket empty rights |

Approval question for each phase: *what happens if this role is compromised?* Install/Remove have the largest blast radius; Deploy and Operate are narrower.

## Install — first-time `sam deploy`

The polyris SAM stack creates 118 resources across 10 AWS services in one CloudFormation stack. The role that runs `sam build && sam deploy` needs permission to create every resource type in the stack.

### Recommended approach: AWS-managed policies

Attach both:

- `arn:aws:iam::aws:policy/PowerUserAccess` — everything except IAM
- `arn:aws:iam::aws:policy/IAMFullAccess` — for the 10 IAM roles + 2 managed policies polyris creates

These two together cover install with reasonable blast radius (no root, no billing, no organization changes). Suitable for a dedicated `polyris-installer` IAM user or CI role.

### Least-privilege alternative

If your org requires enumerated permissions, the installer role needs these services and actions:

| AWS service | Actions | Why |
|-------------|---------|-----|
| `cloudformation` | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `GetTemplate`, `CreateChangeSet`, `ExecuteChangeSet`, `DescribeChangeSet`, `DeleteChangeSet`, `ListStackResources` | SAM builds the CFN change set and executes it |
| `iam` | `CreateRole`, `DeleteRole`, `GetRole`, `PassRole`, `AttachRolePolicy`, `DetachRolePolicy`, `PutRolePolicy`, `DeleteRolePolicy`, `GetRolePolicy`, `TagRole`, `UntagRole`, `CreatePolicy`, `DeletePolicy`, `GetPolicy`, `CreatePolicyVersion`, `DeletePolicyVersion`, `ListPolicyVersions` | 10 roles + 2 managed policies |
| `dynamodb` | `CreateTable`, `DeleteTable`, `UpdateTable`, `DescribeTable`, `TagResource`, `UntagResource`, `UpdateContinuousBackups`, `UpdateTimeToLive` | 8 tables (pipeline-tokens, pipeline-registry, task-events, etc.) |
| `lambda` | `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `DeleteFunction`, `GetFunction`, `AddPermission`, `RemovePermission`, `TagResource`, `PublishVersion` | 8 Lambda functions (console-api, evaluate-deps, notify-*, etc.) |
| `states` | `CreateStateMachine`, `UpdateStateMachine`, `DeleteStateMachine`, `DescribeStateMachine`, `TagResource` | 14 state machines (dependency wrapper, run-task helper, notify-dependents, etc.) |
| `logs` | `CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `TagResource`, `DescribeLogGroups` | 17 log groups |
| `s3` | `CreateBucket`, `DeleteBucket`, `PutBucketPolicy`, `PutBucketPublicAccessBlock`, `PutBucketVersioning`, `PutBucketWebsite`, `PutBucketOwnershipControls`, `PutObject`, `GetObject`, `DeleteObject`, `ListBucket` | Console UI bucket + results bucket |
| `cognito-idp` | `CreateUserPool`, `DeleteUserPool`, `UpdateUserPool`, `CreateUserPoolClient`, `DeleteUserPoolClient`, `UpdateUserPoolClient`, `DescribeUserPool`, `SetUserPoolMfaConfig` | Console API auth |
| `cloudfront` | `CreateDistribution`, `UpdateDistribution`, `DeleteDistribution`, `GetDistribution`, `CreateOriginAccessControl`, `DeleteOriginAccessControl`, `CreateResponseHeadersPolicy`, `DeleteResponseHeadersPolicy`, `CreateFunction`, `DeleteFunction`, `PublishFunction`, `UpdateFunction` | Console UI hosting |
| `apigateway` | `POST`, `GET`, `PUT`, `DELETE`, `PATCH` on `/v2/apis/*` and `/tags/*` | Console API HTTP endpoint |

Scope all `Resource` fields to your polyris stack's namespace where the service supports it (e.g. `arn:aws:states:*:*:stateMachine:${Namespace}-${Stage}-polyris-*`). CloudFormation itself cannot be scoped tighter than the stack name.

## Deploy — per-pipeline `polyris-deploy`

After install, `polyris-deploy` runs from a developer's laptop or a CI job to ship one pipeline at a time. Each pipeline becomes a small CFN stack (`${namespace}-${pipeline}-${stage}`) containing the pipeline's dependency-wrapper state machines and a registration Custom Resource that writes to the `pipeline-registry` DDB table.

Minimum permissions for the developer/CI role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PipelineStackManagement",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateStack",
        "cloudformation:UpdateStack",
        "cloudformation:DeleteStack",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:GetTemplate",
        "cloudformation:CreateChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:ListStackResources"
      ],
      "Resource": "arn:aws:cloudformation:*:*:stack/*-polyris-*/*"
    },
    {
      "Sid": "StateMachineManagement",
      "Effect": "Allow",
      "Action": [
        "states:CreateStateMachine",
        "states:UpdateStateMachine",
        "states:DeleteStateMachine",
        "states:DescribeStateMachine",
        "states:TagResource"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassOrchestrationRole",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::*:role/*-polyris-orchestration-role"
    },
    {
      "Sid": "InvokeRegistrationLambda",
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:*:*:function:*-polyris-registration"
    },
    {
      "Sid": "PipelineLogGroups",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:DescribeLogGroups"
      ],
      "Resource": "arn:aws:logs:*:*:log-group:/aws/vendedlogs/states/*-polyris-*"
    }
  ]
}
```

`iam:PassRole` on the orchestration role is the sensitive one — it lets the caller hand that role to Step Functions. Scope the `Resource` to the exact ARN of `${Namespace}-${Stage}-polyris-orchestration-role` in your account, not a wildcard, if your org's IAM policy allows fine-grained ARNs.

## Operate

Three distinct audiences, each with different permission needs.

### Console user (end user)

Web UI at `https://<distribution>.cloudfront.net/` uses Cognito authentication. Console users authenticate against the Cognito user pool polyris created during install; they do **not** need direct AWS IAM permissions. The Console API Lambda executes with its own role (`console-api-role`) and does all AWS work on the user's behalf.

Onboard a Console user:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$(aws cloudformation describe-stacks \
    --stack-name polyris \
    --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
    --output text)" \
  --username alice@example.com \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true \
  --temporary-password 'TempPass!23' \
  --message-action SUPPRESS
```

Cognito emails the user a one-time password; the Console UI forces them to change it on first login.

### On-call engineer (observability)

Engineers investigating a failing pipeline need read access to CloudWatch logs, Step Functions execution history, and DynamoDB rows. Attach `arn:aws:iam::aws:policy/ReadOnlyAccess` scoped by resource tag if your org supports tag-based conditions:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "logs:GetLogEvents",
      "logs:FilterLogEvents",
      "logs:DescribeLogStreams",
      "logs:DescribeLogGroups",
      "states:GetExecutionHistory",
      "states:DescribeExecution",
      "states:ListExecutions",
      "states:DescribeStateMachine",
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:DescribeTable"
    ],
    "Resource": "*",
    "Condition": {
      "StringLike": {
        "aws:ResourceTag/Namespace": "polyris"
      }
    }
  }]
}
```

### Pipeline task role

The runtime role polyris creates for your task Lambdas / Glue / ECS / Batch containers. Polyris configures this automatically during install — you don't manage it directly. If a pipeline task needs additional permissions beyond the defaults (e.g. read from a specific S3 bucket, invoke a specific external Lambda), attach them to `${Namespace}-${Stage}-polyris-default-task-role` in the AWS console or via a follow-up CFN stack.

Default coverage (as of 1.0.1): Athena queries + Glue Data Catalog CRUD + basic S3 + task-service `StartJobRun` / `RunTask` / `SubmitJob` etc. Full list: [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam).

## Remove — `sam delete` / stack teardown

Same permission surface as Install — the CFN operations that created every resource must be able to delete every resource. Plus one extra concern: S3 buckets must be empty before CFN can delete them.

The polyris stack ships a `BucketCleanupFunction` Custom Resource that empties buckets automatically on stack deletion. The role running `sam delete` needs `lambda:InvokeFunction` on it (already covered by `PowerUserAccess`).

If the cleanup Lambda fails (permission issue, timeout on a very full bucket), you can empty buckets manually before retrying:

```bash
aws s3 rm "s3://${Namespace}-${Stage}-polyris-console-ui" --recursive
aws s3 rm "s3://${Namespace}-${Stage}-polyris-results" --recursive
```

Then re-run `sam delete`.

## IAM roles polyris creates

Ten IAM roles the stack creates during Install. Every role's `RoleName` starts with `${Namespace}-${Stage}-polyris-`. Every trust policy names `states.amazonaws.com`, `lambda.amazonaws.com`, or another AWS service — none accept root or IAM user principals.

| Role | Trust principal | Purpose |
|------|-----------------|---------|
| `polyris-orchestration-role` | Step Functions | Runs the dependency-wrapper state machines. Reads/writes to pipeline-tokens DDB, invokes task Lambdas / helper SFNs / CloudWatch logs. Highest-privilege role in the stack — it's the "conductor" |
| `polyris-default-task-role` | Cross-account trust (from orchestration-role) | The runtime task role. Assumed by task Lambdas / Glue / ECS / Batch when the wrapper starts a task. Covers Athena + Glue + S3 + task-service `StartJobRun` / `RunTask` |
| `polyris-console-api-role` | Lambda | Serves the REST API behind the Console UI. Reads DDB tables, describes SFN executions, sends manual-action writes |
| `polyris-evaluate-deps-role` | Lambda | Called by dependency wrappers to evaluate `trigger_rule` (all_success, all_done, one_success). Read-only on pipeline-tokens |
| `polyris-query-subscriptions-role` | Lambda | Reads the `dep-subscriptions` DDB table to find downstream tasks when an upstream completes |
| `polyris-check-assets-role` | Lambda | Reads `asset-events` and `asset-subscriptions` DDB tables for asset-triggered pipelines |
| `polyris-notify-asset-subscribers-role` | Lambda | Starts downstream pipeline executions when an asset publishes |
| `polyris-notify-role` | Lambda | Publishes to Slack / PagerDuty on alert conditions |
| `polyris-ui-bootstrap-role` | Lambda | One-off role for the CustomResource that copies UI assets into the CloudFront-fronted S3 bucket at Install |
| `polyris-bucket-cleanup-role` | Lambda | One-off role for the CustomResource that empties S3 buckets on Remove |

Two AWS::IAM::ManagedPolicy resources the stack also creates, published for user pipeline tasks that need them:

- `${Namespace}-${Stage}-polyris-task-read` — `dynamodb:GetItem` on `pipeline-tokens` (for `xcom.get()` from task code)
- `${Namespace}-${Stage}-polyris-task-write` — `dynamodb:UpdateItem` on `pipeline-tokens` scoped by `LeadingKeys → output#*` (for `xcom.push()` from task code)

Attach these to the roles your task containers/Lambdas assume — see [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam).

## Cross-account model

If pipeline tasks execute in a different AWS account than the polyris stack (common for orgs that separate control-plane and data-plane accounts), the orchestration role assumes a cross-account role in the target account. See [CROSS_ACCOUNT_ROLES.md](CROSS_ACCOUNT_ROLES.md) for the trust-policy pattern and `polyris-deploy` configuration.
