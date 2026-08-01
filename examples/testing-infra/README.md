# Test resources for the example pipelines

`test-resources.yaml` is a standalone CloudFormation stack that creates **one
reusable resource per task type**, so you can point the example pipelines at real
AWS resources and run them end to end. Deploy it once and reuse it.

Everything is **pay-per-use** — you're only billed while a task actually runs.
EMR is intentionally left out (it has a standing hourly cost); see the note below.

## Deploy

```bash
aws cloudformation deploy \
  --template-file test-resources.yaml \
  --stack-name polyris-test \
  --capabilities CAPABILITY_IAM
```

Then read the outputs (these are the values you paste into the pipelines):

```bash
aws cloudformation describe-stacks --stack-name polyris-test \
  --query 'Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}' --output table
```

## What each output maps to

| Output | Task decorator |
|---|---|
| `LambdaFunctionName` | `@task.lambda_(function_name=...)` |
| `StateMachineArn` | `@task.sfn(arn=...)` |
| `GlueJobName` | `@task.glue(job_name=...)` |
| `AthenaDatabase` / `AthenaOutputLocation` | `@task.athena(database=..., output_location=...)` |
| `EcsClusterName` / `EcsTaskDefinitionArn` | `@task.ecs(cluster=..., task_definition=...)` |
| `Subnets` / `SecurityGroup` | `@task.ecs(subnets=[...], security_groups=[...])` |
| `BatchJobDefinitionArn` / `BatchJobQueueArn` | `@task.batch(job_definition=..., job_queue=...)` |

For ECS, the test container is named **`main`** — matching
`container_overrides={"ContainerOverrides": [{"Name": "main", ...}]}` in
`04_task_types`.

`examples/config.py` is already set to this account (000000000000 / us-east-1,
namespace `polyris-ex`), so `polyris-deploy` in any example folder targets the right
place. Set a `profile` there if you don't use default credentials.

The compute is intentionally trivial: the Lambda returns a small JSON payload, the
state machine succeeds immediately, and the Glue/ECS/Batch jobs just print a line
and exit. That's enough for every task in a pipeline to run green.

## Two things to adjust before running

1. **Athena query.** The examples use `INSERT INTO silver.events SELECT * FROM
   bronze.events`, which needs those tables to exist. This stack creates the
   `analytics` database but not those tables. For a smoke test, swap the query for
   something self-contained, e.g. `SELECT 1`.

2. **Athena needs an S3 grant.** Athena writes query results under the polyris task
   role's identity, which doesn't have S3 access by default. Redeploy this stack with
   your task role name to grant it:

   ```bash
   aws cloudformation deploy --template-file test-resources.yaml \
     --stack-name polyris-test --capabilities CAPABILITY_IAM \
     --parameter-overrides TaskRoleName=polyris-dev-oss-dev-polyris-default-task-role
   ```

3. **Data passing from service tasks.** `xcom.pull("t")` and `event["upstream"]["t"]`
   return whatever task `t` *stored*:
   - For a **Lambda / Step Functions** upstream, that's its **return value** — real
     data. This is where `pull()` shines.
   - For a **Glue / ECS / Athena / Batch / EMR** upstream, the stored output is the
     service's **job metadata** (job-run id, query-execution id, status), *not* row
     data. Those tasks pass real data by **writing to S3 or a table**; the downstream
     reads that location. The examples' docstrings show both patterns.

## EMR is not included

EMR is the one task type this stack leaves out, because a cluster costs money just
by existing (it must stay running so `@task.emr` can add steps). To exercise the EMR
task, create a small cluster yourself and pass its `j-XXXX` id to
`@task.emr(emr_cluster_id=...)` — or just drop the EMR task from `04_task_types`
while smoke-testing.

## Clean up

```bash
aws cloudformation delete-stack --stack-name polyris-test
```

The S3 bucket has a 7-day lifecycle rule, but empty it first if the delete is
blocked by remaining objects.
