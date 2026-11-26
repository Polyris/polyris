# IAM permissions for the polyris lifecycle

Reference for AWS administrators who need to approve a polyris installation and grant the right permissions to the humans and CI roles that install, deploy, operate, and remove it.

Two people hold the permissions, and only the first one changes IAM:

| Person | Runs | Holds | IAM |
|---|---|---|---|
| **Infrastructure engineer** | `polyris-up`, `polyris-down` | The installer's permissions ([Install](#install--polyris-up-or-a-from-source-sam-deploy)) | Creates every IAM role: the platform's (through its stack), and the roles a task may name in `role=` ([give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md)). Attaches the deploy policy to the pipeline authors' roles |
| **Pipeline author** | `polyris-deploy`, `polyris-destroy`, `polyris-validate` | The platform's deploy policy ([Deploy](#deploy--per-pipeline-polyris-deploy)) | None: no IAM write action, apart from the 1.4 migration grant on the old scheduler roles |

Distinct from the **runtime task role** (`${Namespace}-${Stage}-polyris-default-task-role`) that pipelines assume to talk to Glue, Athena, ECS, Batch, and S3 — that role is created by polyris itself during install and documented in [DATA_PASSING.md](../features/DATA_PASSING.md#iam). This doc covers the *operator* permissions to run the polyris tooling, not the *task* permissions polyris configures on your behalf.

## Contents

- [Lifecycle overview](#lifecycle-overview)
- [Install — `polyris-up` (or a from-source `sam deploy`)](#install--polyris-up-or-a-from-source-sam-deploy)
- [Deploy — per-pipeline `polyris-deploy`](#deploy--per-pipeline-polyris-deploy)
- [Operate](#operate)
- [Remove — `polyris-down` / stack teardown](#remove--polyris-down--stack-teardown)
- [IAM roles polyris creates](#iam-roles-polyris-creates)
- [What a pipeline author can reach](#what-a-pipeline-author-can-reach)
- [Isolation is an account](#isolation-is-an-account)
- [Cross-account model](#cross-account-model)

## Lifecycle overview

| Phase | Who runs it | Frequency | AWS-level permissions needed |
|-------|-------------|-----------|------------------------------|
| **Install** | Infrastructure engineer or their CI role | Once per account | Broad — 71 resources across 12 services |
| **Deploy** | Pipeline author or their CI role | Per pipeline, per change | The platform's deploy policy: pipeline stacks, their state machines and schedules, two named roles to pass |
| **Operate** | Console user (Cognito) / on-call engineer | Continuous | None (Console user) or read-only (on-call) |
| **Remove** | Infrastructure engineer | Rare | Same as Install + S3 bucket empty rights |

Approval question for each phase: *what happens if this role is compromised?* Install/Remove have the largest blast radius; Deploy and Operate are narrower.

## Install — `polyris-up` (or a from-source `sam deploy`)

The polyris platform stack creates 71 resources across 12 AWS services in one CloudFormation stack. The role that runs `polyris-up` (or `sam deploy`) needs permission to create every resource type in the stack. `polyris-up` additionally creates and writes the artifacts bucket `polyris-artifacts-<account>-<region>` and calls `sts:GetCallerIdentity` to show the target account.

### Recommended approach: AWS-managed policies

Attach both:

- `arn:aws:iam::aws:policy/PowerUserAccess` — everything except IAM
- `arn:aws:iam::aws:policy/IAMFullAccess` — for the 12 IAM roles + 4 managed policies polyris creates

These two together cover install with reasonable blast radius (no root, no billing, no organization changes). Suitable for a dedicated `polyris-installer` IAM user or CI role.

### Least-privilege alternative

If your org requires enumerated permissions, the installer role needs these services and actions:

| AWS service | Actions | Why |
|-------------|---------|-----|
| `cloudformation` | `CreateStack`, `UpdateStack`, `DeleteStack`, `DescribeStacks`, `DescribeStackEvents`, `GetTemplate`, `CreateChangeSet`, `ExecuteChangeSet`, `DescribeChangeSet`, `DeleteChangeSet`, `ListChangeSets`, `ListStackResources`; `CreateChangeSet` also on `arn:aws:cloudformation:<region>:aws:transform/Serverless-2016-10-31` | `polyris-up` / SAM build the change set and execute it |
| `sts` | `GetCallerIdentity` | `polyris-up` prints the target account before it changes anything |
| `iam` | `CreateRole`, `DeleteRole`, `GetRole`, `PassRole`, `AttachRolePolicy`, `DetachRolePolicy`, `PutRolePolicy`, `DeleteRolePolicy`, `GetRolePolicy`, `ListRolePolicies`, `ListAttachedRolePolicies`, `UpdateAssumeRolePolicy`, `TagRole`, `UntagRole`, `ListRoleTags`, `CreatePolicy`, `DeletePolicy`, `GetPolicy`, `CreatePolicyVersion`, `DeletePolicyVersion`, `ListPolicyVersions`, `GetPolicyVersion`, `ListEntitiesForPolicy` | 12 roles + 4 managed policies |
| `dynamodb` | `CreateTable`, `DeleteTable`, `UpdateTable`, `DescribeTable`, `TagResource`, `UntagResource`, `ListTagsOfResource`, `UpdateContinuousBackups`, `DescribeContinuousBackups`, `UpdateTimeToLive`, `DescribeTimeToLive`, `DescribeContributorInsights`, `DescribeKinesisStreamingDestination`; `Scan` on the `pipeline-registry` table | 8 tables (pipeline-tokens, pipeline-registry, pipeline-specs, task-events, etc.); `polyris-up` reads the registry to list the pipelines to redeploy, and `polyris-down` to refuse a platform whose pipelines are still deployed |
| `lambda` | `CreateFunction`, `UpdateFunctionCode`, `UpdateFunctionConfiguration`, `DeleteFunction`, `GetFunction`, `AddPermission`, `RemovePermission`, `GetPolicy`, `InvokeFunction`, `TagResource`, `UntagResource`, `ListTags`, `PublishVersion`, `CreateEventSourceMapping`, `GetEventSourceMapping`, `UpdateEventSourceMapping`, `DeleteEventSourceMapping`, `ListEventSourceMappings` | 8 Lambda functions (console-api, reconcile, asset-fanout, sweeper, start-run, etc.) and the mappings that feed the asset-events stream to asset-fanout and the tasks table's stream to the dispatcher (`polyris-reconcile`) |
| `states` | `CreateStateMachine`, `UpdateStateMachine`, `DeleteStateMachine`, `DescribeStateMachine`, `TagResource`, `UntagResource`, `ListTagsForResource`; `ListExecutions`, `StopExecution` | 5 state machines (`run_task`, `bulk_backfill` and three test tasks); `polyris-down` stops their running executions until the stack is gone |
| `scheduler` | `CreateSchedule`, `UpdateSchedule`, `DeleteSchedule`, `GetSchedule` | The sweeper's 5-minute schedule |
| `events` | `PutRule`, `DeleteRule`, `DescribeRule`, `PutTargets`, `RemoveTargets`, `ListTargetsByRule`, `TagResource`, `UntagResource`, `ListTagsForResource` | The rule on the default bus that sends an execution's end to the dispatcher (`polyris-execution-end`); `lambda:AddPermission` lets it invoke the function |
| `logs` | `CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `TagResource`, `UntagResource`, `ListTagsForResource`, `DescribeLogGroups` | 10 log groups |
| `s3` | `CreateBucket`, `DeleteBucket`, `PutBucketPolicy`, `PutBucketPublicAccessBlock`, `PutBucketVersioning`, `PutBucketWebsite`, `PutBucketOwnershipControls`, `PutEncryptionConfiguration`, `GetBucketPolicy`, `DeleteBucketPolicy`, `PutBucketTagging`, `GetBucketTagging`, `TagResource`, `UntagResource`, `ListTagsForResource`, `PutObject`, `GetObject`, `DeleteObject`, `ListBucket`, `ListBucketVersions`, `DeleteObjectVersion` | Console UI bucket, results bucket, and the artifacts bucket `polyris-up` uploads release code to |
| `cognito-idp` | `CreateUserPool`, `DeleteUserPool`, `UpdateUserPool`, `CreateUserPoolClient`, `DeleteUserPoolClient`, `UpdateUserPoolClient`, `DescribeUserPool`, `SetUserPoolMfaConfig`, `GetUserPoolMfaConfig`, `AdminCreateUser`, `AdminDeleteUser`, `AdminGetUser`, `TagResource`, `UntagResource`, `ListTagsForResource` | Console API auth; the first console admin when `AdminEmail` is set |
| `cloudfront` | `CreateDistribution`, `CreateDistributionWithTags`, `UpdateDistribution`, `GetDistributionConfig`, `GetOriginAccessControl`, `GetResponseHeadersPolicy`, `DescribeFunction`, `DeleteDistribution`, `GetDistribution`, `CreateOriginAccessControl`, `DeleteOriginAccessControl`, `CreateResponseHeadersPolicy`, `DeleteResponseHeadersPolicy`, `CreateFunction`, `DeleteFunction`, `PublishFunction`, `UpdateFunction`, `TagResource`, `UntagResource`, `ListTagsForResource` | Console UI hosting |
| `apigateway` | `POST`, `GET`, `PUT`, `DELETE`, `PATCH` on `/apis/*` and `/tags/*`; `TagResource`, `UntagResource` | Console API HTTP endpoint |

Every resource in a polyris stack carries its billing tags ([INFRASTRUCTURE.md#tags](INFRASTRUCTURE.md#tags)), so each service also needs its tag, untag and list-tags actions: an upgrade that changes tags fails without them. The lists follow the permissions CloudFormation's resource handlers declare; if a call is still denied, add the action the error names.

Scope all `Resource` fields to your polyris stack's namespace where the service supports it (e.g. `arn:aws:states:*:*:stateMachine:${Namespace}-${Stage}-polyris-*`). CloudFormation itself cannot be scoped tighter than the stack name.

### The checks before any change

`polyris-up` updates a platform from 1.3 or newer; a platform on 1.2 upgrades to 1.3 first with the `polyris-up` of 1.3 ([upgrade-1.2-to-1.3.md](../how-to/upgrade-1.2-to-1.3.md)). Before it uploads or changes anything, it checks the account ([what the checks may refuse](../how-to/manage-the-platform.md#what-the-checks-may-refuse)). It reads the roles that the deployed pipelines' tasks name in `role=`, and every role named like a pipeline's old scheduler role. The two managed policies above cover these reads; a least-privilege installer role needs these actions beside the ones listed above:

| AWS service | Actions | Resource | Why |
|-------------|---------|----------|-----|
| `iam` | `ListRoles` | `*` | Finds the roles named `*-polyris-*-scheduler-role`; `GetRole`, `ListRolePolicies`, `GetRolePolicy` and `ListAttachedRolePolicies` (in the list above) read each one |
| `cloudformation` | `DescribeStackResources` | `*` | Finds the stack that made each such role |
| `cloudformation` | `ListImports` | `*` | `polyris-down` refuses while a pipeline imports the platform's schedule role |
| `dynamodb` | `Query`, `BatchGetItem` | The `pipeline-specs` table | Reads each deployed pipeline's current task specs |

A denied call refuses before anything has changed, and the message names the action.

## Deploy — per-pipeline `polyris-deploy`

After install, `polyris-deploy` runs from a pipeline author's laptop or a CI job to ship one pipeline at a time. Each pipeline becomes a small CFN stack (`${namespace}-${stage}-polyris-${pipeline}`) holding the pipeline's state machine, its log group and, for a scheduled pipeline, its schedule; it holds no IAM role. Before it touches the stack, `polyris-deploy` writes the pipeline's task specs to the `pipeline-specs` table, reads them back and checks them. After the stack deploys, it records the deploy in `pipeline-registry`.

### The deploy policy

The platform stack creates the policy that the role running `polyris-deploy` needs: the managed policy `${Namespace}-${Stage}-polyris-deploy-policy`, whose ARN is the stack output `PipelineDeployPolicyArn`. `polyris-up` prints it at its end:

```text
   Deploy policy for pipeline authors:  arn:aws:iam::123456789012:policy/acme-dev-polyris-deploy-policy
```

The infrastructure engineer attaches it once to each pipeline author's role, or to the CI role that deploys (`attach-user-policy` for an IAM user):

```bash
aws iam attach-role-policy --role-name pipeline-deployer --policy-arn arn:aws:iam::123456789012:policy/acme-dev-polyris-deploy-policy
```

The platform updates the policy with itself: a new release that needs another action ships it, and nobody edits the policy by hand. It allows:

| What | Where |
|---|---|
| CloudFormation stack and change-set calls | Pipeline stacks, `*-polyris-*`; never the platform's own stack |
| Upload of a large template | `cfn-pipeline-templates/` in the platform's results bucket |
| Create, update, delete, describe, start and stop | State machines and executions named `*-polyris-*`, never the platform's own (`run_task`, `bulk_backfill`, the test machines) of any platform; `states:DescribeStateMachine` on any state machine, for the pre-deploy resource check |
| Write the task specs, the registry entry, the asset subscriptions | The platform's own `pipeline-specs`, `pipeline-registry` and `asset-subscriptions` tables |
| Log groups | `/<namespace>/<stage>/polyris/*` |
| Schedules | `default/*-polyris-*-schedule` |
| `iam:PassRole` | Two roles, each to one service: the orchestration role to Step Functions, the schedule role to EventBridge Scheduler |

It allows no IAM write action but one, for 1.4 only: a pipeline deployed before 1.4 has a scheduler role of its own, and its first redeploy on 1.4 deletes that role (`iam:DeleteRole`, `DeleteRolePolicy`, `GetRole`, `ListRolePolicies`, `ListAttachedRolePolicies` on `*-polyris-*-scheduler-role`). A redeploy that fails rolls its schedule back to that role, so the policy may also pass those roles to EventBridge Scheduler. `polyris-up` refuses to install 1.4 unless every role of that name is the one `polyris-deploy` made, and the grant goes in 1.5.

Not in the policy: the reads of the pre-deploy resource check beyond `DescribeStateMachine` (`lambda:GetFunction`, `glue:GetJob`, `ecs:DescribeClusters`, `ecs:DescribeTaskDefinition`, `batch:DescribeJobQueues`, `batch:DescribeJobDefinitions`, `athena:GetWorkGroup`). Without them the check warns that it could not verify a resource and the deploy goes on; add them to the author's role for a full check.

`polyris-destroy` finds the pipeline's state machine among the stack's resources and stops its running executions until the stack is gone (`states:ListExecutions`, `states:StopExecution`): CloudFormation deletes a state machine only once its executions have ended. If it may not, it stops before it deletes anything. Once the stack is gone, it removes the pipeline from the console: its row in `pipeline-registry` and its subscriptions to the assets of its schedule (`dynamodb:DeleteItem`). If that is refused, it says so; grant the action and run it again.

A deploy refused `iam:PassRole` says so and prints the policy to attach. A role that still holds a deploy policy written for an earlier release keeps IAM rights the platform no longer needs: detach it once every author deploys with 1.4 ([manage-the-platform.md](../how-to/manage-the-platform.md#upgrade-to-140)).

## Operate

Three distinct audiences, each with different permission needs.

### Console user (end user)

Web UI at `https://<distribution>.cloudfront.net/` uses Cognito authentication. Console users authenticate against the Cognito user pool polyris created during install; they do **not** need direct AWS IAM permissions. The Console API Lambda executes with its own role (`console-api-role`) and does all AWS work on the user's behalf.

Onboard a Console user:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id "$(aws cloudformation describe-stacks \
    --stack-name acme-dev \
    --query 'Stacks[0].Outputs[?OutputKey==`CognitoUserPoolId`].OutputValue' \
    --output text)" \
  --username alice@example.com \
  --user-attributes Name=email,Value=alice@example.com Name=email_verified,Value=true
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
        "aws:ResourceTag/polyris:namespace": "acme"
      }
    }
  }]
}
```

### Pipeline task role

The role `run_task` assumes to start a task in the platform's account (`role="same"`, the default): it invokes your Lambda, starts your Glue job, ECS task, Batch job, Athena query, EMR step or state machine. Your workload itself runs under its own execution role. Polyris creates this role during install, and every pipeline shares it. A task that needs a right beyond the defaults gets a role of its own instead: [give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md). A permission attached to the default task role reaches every task of every pipeline.

Default coverage: Athena queries + Glue Data Catalog CRUD + basic S3 + task-service `StartJobRun` / `RunTask` / `SubmitJob` etc. Full list: [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam).

## Remove — `polyris-down` / stack teardown

Same permission surface as Install — the CFN operations that created every resource must be able to delete every resource. Plus one extra concern: S3 buckets must be empty before CFN can delete them.

Before deleting, `polyris-down` reads the pipeline registry (`dynamodb:Scan`) and checks each pipeline's state machine (`states:DescribeStateMachine`). It refuses while a pipeline stack imports the platform's schedule role (`cloudformation:ListImports`): destroy the scheduled pipelines first. Before it empties the buckets, and until the stack is gone, it stops the running executions of the platform's state machines (`states:ListExecutions`, `states:StopExecution`): CloudFormation deletes a state machine only once its executions have ended. It empties the console UI bucket itself, the results bucket only with `--delete-data`, and the artifacts bucket when it deletes it (`s3:ListBucketVersions`, `s3:DeleteObject`, `s3:DeleteObjectVersion`). For a from-source `sam delete`, the stack can ship a `BucketCleanupFunction` Custom Resource (`AutoEmptyBucketsOnDelete=true`) that empties both buckets on stack deletion; the role running `sam delete` needs `lambda:InvokeFunction` on it (already covered by `PowerUserAccess`).

If the cleanup Lambda fails (permission issue, timeout on a very full bucket), you can empty buckets manually before retrying:

```bash
aws s3 rm "s3://${Namespace}-${Stage}-polyris-console-ui" --recursive
aws s3 rm "s3://${Namespace}-${Stage}-polyris-results" --recursive
```

Then re-run `polyris-down` (or `sam delete`).

## IAM roles polyris creates

Twelve IAM roles the stack creates during Install. Every role's `RoleName` starts with `${Namespace}-${Stage}-polyris-`. Every trust policy names `states.amazonaws.com`, `lambda.amazonaws.com`, or another AWS service — none accept root or IAM user principals. The default task role also trusts the orchestration role, which assumes it to start a task's work.

| Role | Trust principal | Purpose |
|------|-----------------|---------|
| `polyris-orchestration-role` | Step Functions of this account | Runs `run_task`, `bulk_backfill` and the pipelines' state machines; the stack's highest-privilege role |
| `polyris-default-task-role` | Step Functions and the orchestration role | Starts a task's work in the platform's account (`role="same"`) |
| `polyris-console-api-role` | Lambda | The REST API behind the console |
| `polyris-reconcile-role` | Lambda | The dispatcher, which decides each task of a run and starts it |
| `polyris-asset-fanout-role` | Lambda | Wakes the tasks waiting on new asset data and starts the pipelines scheduled on it |
| `polyris-sweeper-role` | Lambda | Re-checks every due run every 5 minutes |
| `polyris-sweeper-schedule-role` | EventBridge Scheduler | Invokes the sweeper function, and nothing else |
| `polyris-schedule-role` | EventBridge Scheduler of this account | Starts a scheduled pipeline's state machine; every pipeline's schedule uses it |
| `polyris-start-run-role` | Lambda | Writes a run's rows when the run starts |
| `polyris-notify-role` | Lambda | The role of the `notify` function, reserved for alert delivery; no route of this console configures it |
| `polyris-ui-bootstrap-role` | Lambda | Copies the console UI into its bucket at install (stacks installed by `polyris-up`) |
| `polyris-bucket-cleanup-role` | Lambda | Empties the S3 buckets on Remove |

What each role may do:

- **orchestration** reads and writes `pipeline-tokens`, invokes task Lambdas, starts `run_task` and the tasks' state machines, and writes CloudWatch logs. On any resource, it starts the work of the task services (ECS, Batch, Glue, Athena, EMR) and makes the calls of the direct steps: `sns:Publish`, `sqs:SendMessage`, `bedrock:InvokeModel`, S3 object reads, writes and deletes, and DynamoDB item calls, queries and scans. A resource encrypted with a customer-managed KMS key needs its key policy to allow the role. Only Step Functions of your account can assume it, so who may deploy a pipeline decides what it touches ([what a pipeline author can reach](#what-a-pipeline-author-can-reach)). Its limits, which hold for the platform's names in any namespace of the account:
  - it assumes a role of another account, the default task role, or a role of this account under the path `/polyris-tasks/`; any other role of this account is denied;
  - it reads only the secrets of EventBridge connections (`events!connection/*`), which `HttpTask` needs;
  - it changes only the EventBridge rules that Step Functions manages for its `.sync` calls (`StepFunctions*`);
  - it passes a role only to ECS tasks (`iam:PassedToService` = `ecs-tasks.amazonaws.com`);
  - it invokes none of the platform's own functions but `start-run` and `notify`, and never starts, stops or redrives `run_task` or `bulk_backfill`;
  - it never reaches the platform's own data: the console's tokens, the task specs, the dependency pointers, the console's files, the release's artifacts and the pipeline templates.
- **default-task** covers Athena, the Glue Data Catalog, S3 and each task service's start call (`StartJobRun`, `RunTask`, `SubmitJob` and the like); on EMR it adds and watches steps on an existing cluster, and creates or ends none. It has the orchestration role's limits on the platform's functions, state machines, data and files, and passes a role only to ECS tasks. For a task in another account, see [CROSS_ACCOUNT_ROLES.md](CROSS_ACCOUNT_ROLES.md).
- **schedule** starts the state machines named `*-polyris-*`, never the platform's own. Its ARN is exported as `${Namespace}-${Stage}-polyris-schedule-role-arn`, and every scheduled pipeline's stack imports it, so CloudFormation keeps the role while one does.
- **console-api** reads the DynamoDB tables, starts a registered pipeline's run, writes operator actions and stops a stopped task's `run_task`. It starts state machines named `*-polyris-*` and never `run_task`. It asks Step Functions nothing about a run: the run's status is in DynamoDB.
- **reconcile** reads the tasks table's stream. When an execution ends without a write, the rule `polyris-execution-end` invokes it, and it checks that execution with `states:DescribeExecution` on the executions of polyris state machines (`*-polyris-*`), as the sweeper does. The rest is the `polyris-reconcile` policy below.
- **asset-fanout** reads the `asset-events` stream, the `asset:{name}` pointers and the push subscriptions, and each scheduled pipeline's registry entry (`GetItem`). With the `polyris-reconcile` policy it makes one pass of the dispatcher over each run with a waiting task. It deletes a push subscription whose pipeline is no longer scheduled on the asset.
- **sweeper** makes one pass of the dispatcher over each due run, with the `polyris-reconcile` policy. It reads the attention index and keeps its lease and health rows. It checks executions with `states:DescribeExecution` on the executions of polyris state machines (`*-polyris-*`): a task's `run_task` and the run's own, whose pipeline may have a namespace and stage of its own. It retries and catches up push starts: it reads `asset-subscriptions` and each consumer's registry entry (`GetItem`), starts the pipelines scheduled on an asset, and deletes the subscriptions their pipelines no longer have. It checks both streams and their mappings (`dynamodb:DescribeTable` on `asset-events` and `pipeline-tokens`, `lambda:ListEventSourceMappings`).
- **start-run** reads the pipeline's registry entry (`GetItem`) and refuses an execution that is not one of the pipeline's registered state machine. It reads the run's task specs from `pipeline-specs` (`Query`, `BatchGetItem`). It writes the run row, the run's input and one row per task to `pipeline-tokens` with conditional `PutItem` and `UpdateItem`, and reads a refused write back with `GetItem`. It writes the `asset:{name}` pointers of tasks with `wait_for` to `dep-subscriptions`.

Four AWS::IAM::ManagedPolicy resources the stack also creates. One is the dispatcher's own:

- `${Namespace}-${Stage}-polyris-reconcile` — what the dispatcher reads and writes, wherever it runs. Attached to `polyris-reconcile-role`, `polyris-asset-fanout-role` and `polyris-sweeper-role`:
  - in `pipeline-tokens`: run rows, task rows and the run's input, asset facts and warnings; a decision is one transaction, so `dynamodb:ConditionCheckItem` too. It queries `pipeline-execution-index` only for a run whose run row is missing, to stop that run's rows;
  - in `pipeline-specs`: a run's task names (`Query`) and one task's spec at its start (`GetItem`);
  - in `task-events`: each decision's event in the task's timeline;
  - in `dep-subscriptions`: it deletes a task's `asset:` pointers once the task has left its wait, and writes them again for a task that waits again after a Restart (`DeleteItem`, `PutItem`);
  - `states:StartExecution` of the `run_task` state machine only, and `states:SendTaskSuccess`/`SendTaskFailure` to answer a run;
  - `states:StopExecution` on the executions of polyris state machines (`*-polyris-*`), for a Stop of a run: a stopped task's `run_task` and the run's own execution. Every pipeline `polyris-deploy` deploys is named `{namespace}-{stage}-polyris-{dag_id}`, so the pattern covers pipelines of any namespace. IAM does not keep two platforms of one account apart: the engine acts only on the executions its own rows record.

One is for the people who deploy pipelines: `${Namespace}-${Stage}-polyris-deploy-policy`, what `polyris-deploy` needs (the stack's output `PipelineDeployPolicyArn`; [The deploy policy](#the-deploy-policy)).

Two are published for user pipeline tasks that need them:

- `${Namespace}-${Stage}-polyris-task-read` — `dynamodb:GetItem` on `pipeline-tokens` (for `xcom.get()` from task code)
- `${Namespace}-${Stage}-polyris-task-write` — `dynamodb:UpdateItem` on `pipeline-tokens` scoped by `LeadingKeys → output#*` (for `xcom.push()` from task code)

Attach these to the roles your task containers/Lambdas assume — see [DATA_PASSING.md#iam](../features/DATA_PASSING.md#iam).

## What a pipeline author can reach

The deploy policy keeps a pipeline author away from IAM and from the platform itself, not from the account's workloads and data. The shared pipeline role orchestrates the account's workloads and data, so an author reaches what they reach:

- any Glue job (`--extra-py-files`), Batch job (command override), ECS task (task-role override to any role that trusts `ecs-tasks`), EMR cluster (`AddJobFlowSteps`), Lambda and state machine of the account;
- the account's data through the direct steps (S3, DynamoDB, SNS, SQS on any resource — including CDK/SAM asset buckets, so code that their deploy roles later run);
- every EventBridge connection of the account through `HttpTask`;
- any role in **another** account that trusts this one (for example `OrganizationAccountAccessRole` if the platform runs in an Organization's management account, or the roles of `cdk bootstrap --trust`), and any role of this account under `/polyris-tasks/`.

One platform is one trust domain: its authors can disturb each other's pipelines and the platform's run rows, registry and facts (denial of service or confusion, never IAM escalation). polyris keeps no list of the accounts that `role=` may name.

## Isolation is an account

Every `*-polyris-*` name in the platform's roles and policies spans the whole account: two platforms in one account, or two stages, don't keep their authors apart. To keep a team's pipelines, data or roles apart from another's, give each team a platform in an AWS account of its own, and reach shared data from a task with `role=` ([CROSS_ACCOUNT_ROLES.md](CROSS_ACCOUNT_ROLES.md)).

## Cross-account model

If pipeline tasks execute in a different AWS account than the polyris stack (common for orgs that separate control-plane and data-plane accounts), the orchestration role assumes a cross-account role in the target account. See [CROSS_ACCOUNT_ROLES.md](CROSS_ACCOUNT_ROLES.md) for the trust-policy pattern and `polyris-deploy` configuration, and [give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md) for a role in the platform's own account.
