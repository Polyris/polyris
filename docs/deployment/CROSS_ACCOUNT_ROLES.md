# Cross-account roles

How to run a task's work in another AWS account than the platform's: `run_task` assumes a role in that account and starts the work with its credentials.

---

## Setup

### 1. Configure roles in `config.py`

```python
# config.py in your pipelines repo

ENVIRONMENTS = {
    "prod": {
        "namespace": "mycompany",
        "region": "us-east-1",
        "roles": {
            "etl":       "arn:aws:iam::111111111111:role/etl-execution-role",
            "analytics": "arn:aws:iam::222222222222:role/analytics-role",
        },
    },
}
```

### 2. Use in pipeline

```python
@task.sfn(
    arn="arn:aws:states:us-east-1:111111111111:stateMachine:etl-pipeline",
    role="etl",  # key from config.py roles
)
def run_etl():
    pass
```

`role=` works for every task type, not only `sfn`:

```python
@task.glue_job(job_name="cross-acct-etl", role="etl")
def run_glue():
    pass
```

A value that is already a full `arn:aws:iam::...:role/...` is used as it is; otherwise it is a key of `roles` in `config.py`. `role="same"` (the default) runs in the platform's account, as its default task role.

A role of the platform's own account works only under the IAM path `/polyris-tasks/`, which only whoever creates IAM roles can give a role, for example `arn:aws:iam::123456789012:role/polyris-tasks/finance-writer` next to the roles of other accounts above. `polyris-deploy` refuses any other role of the platform's account, and so does `polyris-validate` when `config.py` sets the stage's `account_id`. How to create such a role, with an example of both forms side by side: [give-a-pipeline-its-own-permissions.md](../how-to/give-a-pipeline-its-own-permissions.md).

### 3. IAM trust policy in target account

The target account role must trust the orchestration role of your polyris platform, `<namespace>-<stage>-polyris-orchestration-role` (the platform stack's output `OrchestrationRoleArn`), which starts every task's work (here in account `123456789012`):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::123456789012:role/mycompany-prod-polyris-orchestration-role"
    },
    "Action": "sts:AssumeRole"
  }]
}
```

---

## Environment Variable Override

```bash
export POLYRIS_ROLE_ETL="arn:aws:iam::111111111111:role/etl-role"
```

Priority: env var > `config.py`.

---

## Verify the configuration

`polyris-validate`, in the pipeline's folder, refuses a `role=` key that `config.py` does not define, and, when the stage sets `account_id`, a role of that account outside `/polyris-tasks/`.
