# Cross-Account Roles

Polyris supports executing tasks in different AWS accounts via cross-account IAM roles.

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
```

`role=` works for **every** task type that `run_task` runs, not only `sfn` — the
cross-account credentials are applied by `run_task` for each service
(ADR #106). For example:

```python
@task.glue_job(job_name="cross-acct-etl", role="etl")
@task.lambda_function(function_name="cross-acct-fn", role="etl")
@task.batch_job(job_definition="jd", job_queue="jq", role="etl")
```

A value that is already a full `arn:aws:iam::...:role/...` is used directly;
otherwise it is looked up as a key in `config.py` roles. `role="same"` (the
default) runs in the orchestration account.

### 3. IAM trust policy in target account

The target account role must trust the orchestration role of your polyris platform, `<namespace>-<stage>-polyris-orchestration-role`, which runs `run_task` (here in account `123456789012`):

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

## Verify Configuration

```bash
python3 -c "
from polyris.config import config
c = config.for_stage('prod')
print(c.roles['etl'])
"
```
