# Give one pipeline its own permissions

A task starts its work as the platform's default task role, which is the same for every pipeline. When one task needs a right that role lacks, the infrastructure engineer (whoever runs `polyris-up` and may create IAM roles) creates a role for it, and the pipeline author names that role in the task's `role=`. This guide has one part for each of them.

The example runs end to end: the `reports` pipeline writes its daily report to the S3 bucket `finance-reports` through the role `polyris-tasks/finance-writer`. The platform is `acme-prod` in account `123456789012`, region `us-east-1`.

## Which roles a task may use

| The role is in | Its ARN | Who creates it |
|---|---|---|
| The platform's account | `arn:aws:iam::123456789012:role/polyris-tasks/finance-writer`: the IAM path must be `/polyris-tasks/` | The infrastructure engineer |
| Another account | `arn:aws:iam::444455556666:role/finance-writer`: any path | An administrator of that account |

`polyris-deploy` refuses any other role of the platform's account. A role gets its path when it is created, and only someone allowed to create IAM roles can give it one, so a pipeline can use only the roles somebody made for pipelines, never an administrator's role.

The role makes the call that starts the task's work, so it needs that service's actions:

- **Athena** runs a query with the rights of whoever starts it: an `@task.athena_query` task reads its tables and writes its results as this role. This guide's example is such a task.
- **Lambda, Glue, ECS and Batch** run your code as the function's or job's own execution role. Give that role the data access, and this role the right to start the work (for example `lambda:InvokeFunction` on the function).

## For the infrastructure engineer

You need the right to create IAM roles in the account where the role lives. Pipeline authors don't have it: the platform's deploy policy grants them no IAM write.

### 1. Find the orchestration role

The new role trusts the platform's orchestration role, which starts every task's work. `polyris-up` prints its ARN at its end:

```text
   Orchestration role (a task's own role trusts it):  arn:aws:iam::123456789012:role/acme-prod-polyris-orchestration-role
```

It is also the platform stack's output `OrchestrationRoleArn`.

### 2. Create the role under `/polyris-tasks/`

Save the trust policy as `trust.json`, with the ARN from step 1:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"AWS": "arn:aws:iam::123456789012:role/acme-prod-polyris-orchestration-role"},
    "Action": "sts:AssumeRole"
  }]
}
```

Create the role in the platform's account:

```bash
aws iam create-role --path /polyris-tasks/ --role-name finance-writer --assume-role-policy-document file://trust.json
```

IAM can't change a role's path later. A role created without `--path /polyris-tasks/` is refused: create it again with the path.

### 3. Give it only what the task needs

The task `export_daily` runs an Athena query on the table `finance.sales`, whose data is in the bucket `acme-sales`, and writes the result under `s3://finance-reports/daily/`. Save its permissions as `permissions.json`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RunTheQuery",
      "Effect": "Allow",
      "Action": ["athena:StartQueryExecution", "athena:GetQueryExecution", "athena:StopQueryExecution"],
      "Resource": "arn:aws:athena:us-east-1:123456789012:workgroup/primary"
    },
    {
      "Sid": "ReadTheCatalog",
      "Effect": "Allow",
      "Action": ["glue:GetDatabase", "glue:GetTable", "glue:GetPartitions"],
      "Resource": [
        "arn:aws:glue:us-east-1:123456789012:catalog",
        "arn:aws:glue:us-east-1:123456789012:database/finance",
        "arn:aws:glue:us-east-1:123456789012:table/finance/*"
      ]
    },
    {
      "Sid": "ReadTheSales",
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:GetObject"],
      "Resource": ["arn:aws:s3:::acme-sales", "arn:aws:s3:::acme-sales/*"]
    },
    {
      "Sid": "WriteTheReport",
      "Effect": "Allow",
      "Action": ["s3:GetBucketLocation", "s3:ListBucket", "s3:PutObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::finance-reports", "arn:aws:s3:::finance-reports/daily/*"]
    }
  ]
}
```

```bash
aws iam put-role-policy --role-name finance-writer --policy-name export-daily --policy-document file://permissions.json
```

### 4. Hand the ARN to the pipeline author

```text
arn:aws:iam::123456789012:role/polyris-tasks/finance-writer
```

### The same role in another account

To keep the data in another account (here `444455556666`), its administrator follows the same steps there: the same `trust.json`, which names the orchestration role of the platform's account; `aws iam create-role` with any path or none; the permissions on that account's resources. Side by side:

| | Platform's account | Another account |
|---|---|---|
| Role ARN | `arn:aws:iam::123456789012:role/polyris-tasks/finance-writer` | `arn:aws:iam::444455556666:role/finance-writer` |
| Path | `/polyris-tasks/`, required | any |
| Trust policy | the platform's orchestration role | the same |
| Created by | the platform's infrastructure engineer | that account's administrator |

More on the other account's side: [CROSS_ACCOUNT_ROLES.md](../deployment/CROSS_ACCOUNT_ROLES.md).

## For the pipeline author

### 1. Name the role in `config.py`

Add the ARN to the stage's `roles`. Set `account_id` too: with it, `polyris-validate` checks the role before you deploy.

```python
DEFAULT_STAGE = "prod"

ENVIRONMENTS = {
    "prod": {
        "stack_name": "acme-prod",
        "namespace": "acme",
        "region": "us-east-1",
        "account_id": "123456789012",
        "roles": {
            "finance_writer": "arn:aws:iam::123456789012:role/polyris-tasks/finance-writer",
            # the same role in another account:
            # "finance_writer": "arn:aws:iam::444455556666:role/finance-writer",
        },
    },
}
```

### 2. Use it in the task

```python
from polyris import DAG, task

with DAG("reports", schedule="@daily") as dag:

    @task.athena_query(
        query_string="SELECT region, SUM(amount) AS revenue FROM sales GROUP BY region",
        database="finance",
        output_location="s3://finance-reports/daily/",
        role="finance_writer",
    )
    def export_daily():
        pass

    export_daily()
```

`role=` also takes the full ARN instead of a key of `roles`. Only this task runs as `finance-writer`; the pipeline's other tasks keep the default task role.

### 3. Check it

Run `polyris-validate` in the pipeline's folder. A role of the platform's account outside `/polyris-tasks/` is refused with the fix:

```text
❌ Task 'export_daily': role arn:aws:iam::123456789012:role/finance-writer is in the platform's account, and polyris may use only roles of this account under the path /polyris-tasks/. Ask your platform admin to create the role under that path (docs: Give one pipeline its own permissions) and use its ARN, e.g. arn:aws:iam::123456789012:role/polyris-tasks/finance-writer. Roles in other accounts work as before.
```

Without `account_id`, `polyris-validate` can't tell which account is the platform's; `polyris-deploy` checks the role against your credentials' account and refuses it with the same message before it changes anything.

### 4. Deploy and run

```bash
polyris-deploy --stage prod
```

If the infrastructure engineer's part is wrong, the task fails at its first run. Open the task in the console: its **Details** tab shows the error from AWS. An error about assuming the role means its trust policy doesn't name the orchestration role (step 2); an `AccessDenied` that names another action, such as `athena:StartQueryExecution`, is a permission the role lacks (step 3). Send the error to the infrastructure engineer, then **Restart** the task.

## What the path doesn't do

- **It doesn't tie a role to one pipeline.** Every pipeline author of this platform may name `finance-writer` in a `role=`: the path decides which roles pipelines may use, not which pipeline uses one. Give a role under `/polyris-tasks/` only what you would give every author of the platform, and review changes to `role=` and `roles` as you review any access change.
- **It doesn't protect a role's trust policy.** Name the platform's exact orchestration role in it, never an account root (`arn:aws:iam::123456789012:root`) and never `"Principal": "*"`. This matters most for a role in another account: the platform's limits stop at its own account, so that role's trust policy is the only thing that decides who assumes it.
- **It doesn't separate two platforms of one account.** A `dev` and a `prod` platform in the same account share the path; only each role's trust policy, naming one platform's orchestration role, keeps a role to that platform. To keep teams or stages apart, give each a platform in an account of its own ([IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#isolation-is-an-account)).
- **It doesn't stop whoever may create IAM roles.** Anyone allowed to create a role can create one under `/polyris-tasks/` with any rights. The right to create roles is the boundary: keep it with the infrastructure engineer.
- **It doesn't keep other roles out of the bucket.** The platform's shared roles may write to the buckets of the account ([what a pipeline author can reach](../deployment/IAM_PERMISSIONS.md#what-a-pipeline-author-can-reach)). To make `finance-writer` the only writer of `finance-reports`, add a bucket policy that denies `s3:PutObject` to every principal whose `aws:PrincipalArn` is not this role.
