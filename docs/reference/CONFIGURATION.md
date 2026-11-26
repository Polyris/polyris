# Configuration

`config.py` holds a pipeline project's settings per stage: which platform stack, namespace, region, profile, account and roles `polyris-deploy` uses. With it, `polyris-deploy --stage prod` picks up everything for prod.

`config.py` is optional while there is one platform stack in the region: without it, `polyris-deploy` finds the stack `polyris-up` installed and takes its namespace and stage. Add it when you have several stages, want an account guard (`account_id`), or name roles for your tasks.

---

## Quick start

```bash
polyris-init --project            # writes pipelines/config.py
cd pipelines && polyris-init my-pipeline
```

Or write `config.py` yourself, at the root of the folder that holds your pipeline folders:

```python
# pipelines/config.py

# The keys ("dev", "prod") are the stage names: --stage picks one.
ENVIRONMENTS = {
    "dev": {
        "stack_name": "mycompany-dev",  # the platform stack polyris-up created: <namespace>-<stage>
        "namespace": "mycompany",
        "region": "us-east-1",
        "account_id": "111111111111",
        # "profile": "my-dev-profile",  # optional
    },
    "prod": {
        "stack_name": "mycompany-prod",
        "namespace": "mycompany",
        "region": "us-east-1",
        "account_id": "222222222222",
        # "profile": "my-prod-profile",
        # "roles": {
        #     "etl": "arn:aws:iam::222222222222:role/polyris-tasks/etl-role",
        # },
    },
}

DEFAULT_STAGE = "dev"  # the stage picked when --stage is not passed
```

---

## Fields

The stage is the key (`"dev"`, `"prod"`); there is no `stage` field. Inside each stage:

| Field | Read by | Purpose |
|-------|---------|---------|
| `stack_name` | `polyris-deploy` | The platform stack `polyris-up` created, `<namespace>-<stage>`. `polyris-deploy` reads its outputs: the start-run function, the orchestration role, the table names and the results bucket. Optional while one platform is in the region |
| `namespace` | `polyris-deploy` | The prefix of the pipeline stacks: `{namespace}-{stage}-polyris-{dag_id}`. Usually the namespace you gave `polyris-up` |
| `region` | `polyris-deploy` | The region of the platform stack. Every AWS call uses it |
| `profile` | `polyris-deploy` | A named AWS profile. Optional |
| `account_id` | `polyris-deploy`, `polyris-validate` | A guard: `polyris-deploy` checks the account of your credentials (`sts:GetCallerIdentity`) and refuses to deploy to another. `polyris-validate` uses it to check `role=` (below). Optional, recommended |
| `roles` | `polyris-deploy`, `polyris-validate` | Role ARNs a task names by key (`@task.sfn(role="etl")`): a role in another account, or a role of the platform's account under the path `/polyris-tasks/`. `polyris-deploy` puts the ARN in the generated pipeline; `polyris-validate` rejects a key that is not here. `POLYRIS_ROLE_<KEY>` overrides one |

At the module level, `DEFAULT_STAGE` is the stage picked when `--stage` is not passed; it must be one of the `ENVIRONMENTS` keys.

---

## Which `config.py`, and what wins

`polyris-deploy` reads the `config.py` that `POLYRIS_CONFIG` names, or else the first `config.py` with `ENVIRONMENTS` from the current folder up to the project root (`.git` or `pyproject.toml`). So a pipeline folder inside `pipelines/` finds `pipelines/config.py`. It stops with the reason, before any AWS call, when it cannot use that config. That happens when `POLYRIS_CONFIG` names a missing file, a file without `ENVIRONMENTS` or one that does not parse. It also happens when a `config.py` it found does not parse or fails when run.

Per field, the first that is set wins:

1. **Command-line options**: `--stage`, `--stack`, `--region`, `--profile`
2. **Environment variables**: `POLYRIS_STAGE`, `POLYRIS_STACK`, `POLYRIS_NAMESPACE`, `POLYRIS_REGION`, `POLYRIS_PROFILE`, `AWS_REGION`, `AWS_PROFILE`
3. **`config.py`**: `ENVIRONMENTS[stage]`

An option that overrides `config.py` prints a warning:

```
⚠️  --region us-west-2 overrides config.py (us-east-1). Continuing.
```

Every deploy prints the resolved target before its first AWS write:

```
── Deploy target ──────────────────────────────────────────────────
   dag:             daily_orders
   stage:           dev
   config:          /home/me/pipelines/config.py
   platform stack:  mycompany-dev    ← reads CloudFormation outputs from here
   namespace:       mycompany
   region:          us-east-1
   profile:         my-dev-profile
   account:         123456789012
   pipeline stack:  mycompany-dev-polyris-daily_orders
───────────────────────────────────────────────────────────────────
```

---

## Roles for tasks

A task's `role=` names one of two kinds of role: a role in another account, or a role of the platform's account whose IAM path is `/polyris-tasks/`. `polyris-deploy` refuses any other role of the platform's account, and so does `polyris-validate` when the stage sets `account_id`.

```python
ENVIRONMENTS = {
    "prod": {
        "stack_name": "mycompany-prod",
        "namespace": "mycompany",
        "region": "us-east-1",
        "account_id": "222222222222",
        "roles": {
            # a role of the platform's account, under /polyris-tasks/
            "etl": "arn:aws:iam::222222222222:role/polyris-tasks/etl-execution-role",
            # a role in another account
            "analytics": "arn:aws:iam::456789012345:role/analytics-role",
        },
    },
}
```

In the pipeline:

```python
@task.sfn(
    arn="arn:aws:states:us-east-1:123456789012:stateMachine:my-etl-job",
    role="etl",  # a key of config.py's roles
)
```

Setting up the roles: [CROSS_ACCOUNT_ROLES.md](../deployment/CROSS_ACCOUNT_ROLES.md).

---

## Several stages

Install a platform per stage with `polyris-up` ([manage-the-platform.md](../how-to/manage-the-platform.md#add-a-stage)), add the stage to `ENVIRONMENTS`, and deploy to it:

```bash
polyris-deploy --stage dev
polyris-deploy --stage prod
polyris-deploy --stage prod --profile my-prod-profile  # another profile
```

---

## CI/CD

Environment variables replace `config.py` settings, for example in GitHub Actions:

```yaml
- name: Deploy
  env:
    POLYRIS_STACK: mycompany-prod   # or POLYRIS_STAGE and a config.py
    # POLYRIS_CONFIG: ci/config.py  # a config.py elsewhere; it must exist
    POLYRIS_REGION: us-east-1
    AWS_PROFILE: prod
  run: polyris-deploy
```

A platform deployed from a checkout with `sam deploy`: [FROM_SOURCE.md](../deployment/FROM_SOURCE.md#pipelines-on-a-stack-deployed-with-sam). Every command's options: [CLI.md](CLI.md).
