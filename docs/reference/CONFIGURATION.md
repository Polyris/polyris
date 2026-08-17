# Configuration

Polyris uses `config.py` for per-stage pipeline project settings. Read by
`polyris-deploy` when it deploys a pipeline — a single `--stage <name>` picks
up stack name, region, profile, and roles for that environment.

`ui/deploy.sh` does **not** read `config.py` — UI deploy is a per-stack
action independent of any pipeline; pass the stack + region as CLI args.

---

## Quick Start

```bash
# Generate config.py template
polyris-init --project
```

Or create manually at `pipelines/config.py` in the monorepo layout (or at
the pipelines-repo root if pipelines live separately):

```python
# pipelines/config.py

# The dict KEYS ("dev", "prod") are the stage names — that's what --stage picks.
# There is no separate "stage" field inside the per-stage dict.
ENVIRONMENTS = {
    "dev": {
        "stack_name": "mycompany-dev",  # SAM stack (matches samconfig.toml)
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
        #     "etl": "arn:aws:iam::123456789012:role/etl-role",
        # },
    },
}

DEFAULT_STAGE = "dev"  # stage KEY picked when --stage is not passed
```

---

## What each field does

The stage itself is the dict KEY (`"dev"`, `"prod"`). The table below lists
the fields *inside* each stage's dict, plus the module-level `DEFAULT_STAGE`.

| Field | Read by | Purpose |
|-------|---------|---------|
| `stack_name` | `polyris-deploy` | The SAM CloudFormation stack `sam deploy` created. `polyris-deploy` calls `describe_stacks(<stack_name>)` to fetch wrapper ARN, orchestration role ARN, DynamoDB table names, and results bucket. **Must match `stack_name` in `sam/samconfig.toml`.** |
| `namespace` | `polyris-deploy` | Prefix for the *pipeline* stacks polyris-deploy creates: `{namespace}-{stage}-polyris-{dag_id}` (where `{stage}` is the dict key). Does not need to match `stack_name`. |
| `region` | `polyris-deploy` | The AWS region of the SAM stack. All SDK calls use this. |
| `profile` | `polyris-deploy` | Named profile from `~/.aws/credentials`. Optional. |
| `account_id` | `polyris-deploy` | Guard — `polyris-deploy` runs `sts:GetCallerIdentity` and refuses to deploy if credentials point at a different account. Optional but recommended. |
| `roles` | task code (at runtime) | Runtime role ARNs referenced by pipeline tasks via `@task.sfn(role="etl")`. Not used at deploy time. |
| `DEFAULT_STAGE` (module-level) | `polyris-deploy` | The stage KEY picked when `--stage` is not passed. Must be one of the `ENVIRONMENTS` keys. |

---

## Which command reads what

| Command | Reads from | Fields needed |
|---------|------------|---------------|
| `sam deploy` | `sam/samconfig.toml` | `stack_name`, `region`, `profile`, `Namespace`, `Stage` |
| `polyris-deploy --stage <s>` | `config.py` → `ENVIRONMENTS[s]` | `stack_name`, `namespace`, `region`, `profile` (optional), `account_id` (optional) |
| `ui/deploy.sh <stack> <region>` | CLI args (no config file) | Runs independent of `config.py`; you pass what it needs. |

**Key overlap:** the `stack_name` in `config.py` must equal the `stack_name` in
`sam/samconfig.toml`. Polyris cannot cross-check them because the two files may
live in different repositories.

---

## Priority for polyris-deploy

Per field, in order:

1. **CLI arguments** — `--stage`, `--stack`, `--region`, `--profile`
2. **Environment variables** — `POLYRIS_STAGE`, `POLYRIS_STACK`, `POLYRIS_REGION`, `POLYRIS_PROFILE`, `AWS_REGION`, `AWS_PROFILE`
3. **config.py** — `ENVIRONMENTS[stage]`

CLI arguments that override config values print a loud warning:

```
⚠️  --region us-west-2 overrides config.py (us-east-1). Continuing.
```

Every deploy also prints a resolved-target summary before the first AWS write:

```
── Deploy target ──────────────────────────────────────────────────
   dag:             daily_orders
   stage:           dev
   sam stack:       mycompany-dev    ← reads CloudFormation outputs from here
   namespace:       mycompany
   region:          us-east-1
   profile:         my-dev-profile
   pipeline stack:  mycompany-dev-polyris-daily_orders
───────────────────────────────────────────────────────────────────
```

---

## Cross-Account Roles

```python
ENVIRONMENTS = {
    "prod": {
        "stack_name": "mycompany-prod",
        "namespace": "mycompany",
        "stage": "prod",
        "region": "us-east-1",
        "roles": {
            "etl": "arn:aws:iam::123456789012:role/etl-execution-role",
            "analytics": "arn:aws:iam::456789012345:role/analytics-role",
        },
    },
}
```

Usage in pipeline:
```python
@task.sfn(
    arn="arn:aws:states:us-east-1:...",
    role="etl",  # key from config.py roles
)
```

---

## Multi-Stage Deploy

```bash
# Deploy pipelines
polyris-deploy --stage dev
polyris-deploy --stage prod
polyris-deploy --stage prod --profile my-prod-profile  # override profile

# Deploy the console UI — pass the SAM stack + region explicitly (deploy.sh
# does NOT read config.py; UI deploy is a per-stack action).
cd ui && ./deploy.sh myorg-dev  us-east-1 --profile polyris-dev
cd ui && ./deploy.sh myorg-prod us-east-1 --profile polyris-prod
```

---

## Monorepo vs split-repo

`polyris-deploy` uses `--stage <name>` to read `config.py`. `ui/deploy.sh` is
independent — it takes the SAM stack + region positionally and does not read
`config.py`. Both work in either layout:

- **Monorepo** (pipelines + SAM + UI in one repo). `scripts/setup-polyris.sh`
  writes `pipelines/config.py`; `polyris.config` walks up from the pipeline
  directory to find it, so `polyris-deploy` works from any pipeline subdir.
- **Split-repo** (pipelines in repo A, SAM in repo B, UI in repo C). Each repo
  that runs `polyris-deploy` needs its own `config.py`. Keep the `stack_name`
  field in sync with the SAM `samconfig.toml` — polyris cannot see across
  repositories.

---

## Environment Variables (CI/CD)

For GitHub Actions or other CI systems:

```yaml
- name: Deploy
  env:
    POLYRIS_STACK: mycompany-prod   # or POLYRIS_STAGE + a config.py
    POLYRIS_REGION: us-east-1
    AWS_PROFILE: prod
  run: polyris-deploy
```

---

See [CLI.md](CLI.md) for full command options reference.
