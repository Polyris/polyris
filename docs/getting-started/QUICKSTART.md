# Complete Setup Guide (From Scratch)

Deploy polyris to a blank AWS account. ~10-15 minutes.

Two paths after the prerequisites: run the one-command script, or step
through it manually. Same result either way — pick whichever fits.

---

## Prerequisites

Same for both installation paths below.

### AWS CLI
```bash
# macOS
brew install awscli

# Linux — official v2 installer (pip installs the deprecated v1)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip && sudo ./aws/install
rm -rf awscliv2.zip aws/

# Configure credentials
aws configure --profile polyris-dev
# Enter: Access Key, Secret, region (e.g. us-east-1), output format (json)
```

### AWS SAM CLI
```bash
# macOS
brew tap aws/tap && brew install aws-sam-cli

# Linux
pip install aws-sam-cli

# Verify
sam --version   # must be >= 1.50.0
```

### Node.js (for UI build)
```bash
# via nvm (recommended)
nvm install 22 && nvm use 22

node --version  # must be >= 18.18 (22 LTS recommended)
```

### Clone the repo

Two ways: run the one-line installer, or `git clone` yourself.

**Installer** — checks prereqs, clones into `~/polyris` (override with
`POLYRIS_DIR`), then prints the next command:

```bash
curl -fsSL https://raw.githubusercontent.com/Polyris/polyris/main/scripts/install.sh | bash
```

**Manual clone** — same result, if you prefer:

```bash
git clone https://github.com/Polyris/polyris.git
cd polyris
```

Either way, after this step you're at the repo root and can proceed with
Option A (script) or Option B (manual) below.

---

## Option A — one-command install

Meant for the monorepo layout (SAM + UI + pipelines in one checkout).

### Install infra + UI together

```bash
./scripts/setup-polyris.sh
```

Interactive. Prompts for AWS profile, region, namespace, stage, stack name,
whether to enable Cognito auth (and if yes, admin email + username). Then:

1. Writes `sam/samconfig.toml` and `pipelines/config.py`
2. `pip install -e .` for the polyris SDK (so you get `polyris-init` and
   `polyris-deploy` for the pipeline step)
3. Runs `sam build && sam deploy` (~10-15 min)
4. Builds and deploys the UI (`npm ci && npm run build`, then `ui/deploy.sh`
   with explicit positional args)
5. Scaffolds `pipelines/hello-world/dag.py` — a minimal, immediately-runnable
   pipeline pointing at the built-in TestQuick SFN the SAM stack ships
6. Creates the first Cognito admin user (only if auth is enabled) and prints
   the temp password
7. Prints the Console URL / API URL / login and the one command to deploy
   the scaffolded pipeline

The script re-prompts before overwriting existing files, so re-running is
safe.

### Add another Cognito user later

```bash
./scripts/setup-polyris.sh --create-user
```

Prompts for email + username and asks whether to set a permanent password
now or generate a temp one. Reads `sam/samconfig.toml` for the stack.

### Delete infra + UI together

```bash
./scripts/setup-polyris.sh --delete
```

Resolves stack / region / profile from CLI flags → `sam/samconfig.toml` →
interactive prompt, asks for confirmation, then runs `sam delete`. If the
stack was deployed with `AutoEmptyBucketsOnDelete=true` (setup asks for
this; default is **no**), `ResultsBucket` and `ConsoleUiBucket` are
auto-emptied by the built-in `BucketCleanup` Lambda. Otherwise `sam
delete` will fail on the non-empty buckets and you must empty them
manually first (`aws s3 rm --recursive s3://<bucket>`). Local
`sam/samconfig.toml`, `pipelines/config.py`, and any pipelines you
scaffolded are kept so you can redeploy.

> **Warning:** deletion is permanent. `ResultsBucket` holds task xcom
> output; once emptied it's gone.

---

## Option B — install each part separately

The rest of this guide walks the same steps manually. Use this if you want
to see what the script does, or if your layout isn't a monorepo (UI in a
different repo, pipelines separate, …).

---

## Step 1: Configure

```bash
cd sam
cp samconfig.toml.example samconfig.toml
```

Edit `samconfig.toml` — set the required values:

```toml
[default.deploy.parameters]
stack_name        = "myorg-dev"        # CloudFormation stack name
region            = "us-east-1"
profile           = "polyris-dev"      # AWS CLI profile from `aws configure --profile …`
capabilities      = "CAPABILITY_IAM CAPABILITY_NAMED_IAM"
resolve_s3        = true
confirm_changeset = false
parameter_overrides = [
  "Namespace=myorg",              # prefix for all resource names: myorg-dev-polyris-*
  "Stage=dev",                    # dev | prod
  "AwsRegion=us-east-1",
  "EnableCognitoAuth=true",       # set false to skip auth during initial testing
  "AutoEmptyBucketsOnDelete=true",# DEV/TEST — lets `sam delete` clean up the
                                  # S3 buckets automatically. NEVER enable in
                                  # prod without a backup (silently wipes
                                  # every task result in ResultsBucket).
]
```

Namespace + Stage together must be **≤ 25 characters** — the longest IAM role
name in the template is `{namespace}-{stage}-polyris-notify-asset-subscribers-role`,
and AWS caps role names at 64. The script guardrails this; the manual path
doesn't, so watch the length here.

Everything else in `samconfig.toml` (custom domain, log levels) is optional.

> **Your stack name lives in `samconfig.toml`** — the `stack_name` field (the
> example above sets `myorg-dev`). The `sam` commands read it straight from
> there, so that file is the single source of truth. Two things **can't** read
> it, though: the UI deploy (`./deploy.sh`) and the AWS CLI output lookups
> (`describe-stacks`). Give them the same name or they fail with
> *"Stack … does not exist"*.

Set your values as shell variables **once in this terminal** so the commands below
stay copy-pasteable. Keep `STACK_NAME` equal to `stack_name` in `samconfig.toml`:

```bash
STACK_NAME=myorg-dev       # = stack_name in samconfig.toml
AWS_REGION=us-east-1       # = AwsRegion in samconfig.toml
AWS_PROFILE=polyris-dev    # the profile from `aws configure --profile …`
```

The UI deploy and the CLI lookups below take these explicitly, so nothing rides on
a hidden default. Opening a new terminal later? Set them again first.

---

## Step 2: Deploy Infrastructure

```bash
cd sam
sam build && sam deploy --profile "$AWS_PROFILE"
# sam reads the stack name, region, and parameters from samconfig.toml (the single
# source of truth). Pass only --profile — it's the one thing not stored there.
```

First deploy takes ~5-10 minutes. Creates:
- 8 DynamoDB tables
- 16 Step Functions state machines (13 templates + 3 test SFNs)
- 6 Lambda functions
- API Gateway, Cognito, S3, CloudFront

Stack outputs (wrapper ARN, table names, bucket, Cognito IDs, …) are visible via:

```bash
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" \
  --output table \
  --profile "$AWS_PROFILE"
```

`polyris-deploy` and `ui/deploy.sh` both read the same outputs at deploy time —
you don't have to copy anything by hand.

---

## Step 3: Deploy UI

```bash
cd ../ui
npm ci && npm run build
# Pass the stack name + region positionally — deploy.sh does not read
# samconfig.toml, so omitting either exits with "stack-name and region are required".
./deploy.sh "$STACK_NAME" "$AWS_REGION" ./out --profile "$AWS_PROFILE"
```

> `ui/deploy.sh` takes stack + region positionally (and `--profile` as a
> flag). It deliberately does **not** read `pipelines/config.py` — UI deploy
> is a per-stack action; the pipeline config is a per-pipeline concern.
> Pass the same values you used for `sam deploy`.

The script reads CloudFormation outputs and generates `config.js` automatically — including auth settings. **To disable auth or change any SAM parameter, update `samconfig.toml`, run `sam deploy`, then rerun `./deploy.sh`. No frontend code changes needed.**

Output:
```
✅ UI deployed!
   URL: https://xxxx.cloudfront.net
   API: https://xxxx.execute-api.us-east-1.amazonaws.com/dev
```

Open the URL in your browser. You should see the polyris Console.

> **Prefer to run the Console locally?** Create `ui/.env.local` with the full API
> Gateway invoke URL — stage **and** `/api` — then start the dev server:
>
> ```env
> NEXT_PUBLIC_API_URL=https://<id>.execute-api.<region>.amazonaws.com/dev/api
> NEXT_PUBLIC_AUTH_ENABLED=false
> ```
> ```bash
> cd ui && npm run dev      # http://localhost:3000
> ```
>
> Use **`.env.local`** for local dev — not `ui/public/config.js` (that one is only
> for the deployed site, auto-generated by `ui/deploy.sh`; leave it alone). CORS is
> open, so localhost talks to the deployed API directly. Full guide, including
> Cognito, is in `docs/operations/UI.md` → *Local UI against a deployed API*.

---

## Step 4: Create First User (Cognito)

Skip if you set `EnableCognitoAuth=false`.

### With the script (recommended)

```bash
./scripts/setup-polyris.sh --create-user
```

Interactive: prompts for email + username, then asks whether to set a
permanent password immediately or generate a temp one (user changes on
first login). Reads `sam/samconfig.toml` for stack / region / profile so
you don't have to pass them.

Run it as many times as you want to add more users.

### Manually with the AWS CLI

```bash
# Get User Pool ID
POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs[?OutputKey=='CognitoUserPoolId'].OutputValue" \
  --output text \
  --profile "$AWS_PROFILE")

# Create user
aws cognito-idp admin-create-user \
  --user-pool-id $POOL_ID \
  --username admin@example.com \
  --user-attributes Name=email,Value=admin@example.com Name=email_verified,Value=true \
  --temporary-password "TempPass123!" \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE"

# Set permanent password
aws cognito-idp admin-set-user-password \
  --user-pool-id $POOL_ID \
  --username admin@example.com \
  --password "YourSecurePassword123!" \
  --permanent \
  --region "$AWS_REGION" \
  --profile "$AWS_PROFILE"
```

Log in at the CloudFront URL.

> **For API/CLI/CI access** (not the browser), use a Cognito access token —
> `scripts/get-e2e-token.sh` obtains one. (Auth is enforced only when
> `AUTH_ENABLED=true`.)

---

## Deploy Your First Pipeline

Applies to **both** installation paths. The infra + UI are up; pipelines are
what actually do work.

### Install the SDK

**If you used the script (Option A):** the SDK is already installed AND
`pipelines/config.py` + `pipelines/hello-world/dag.py` are already
scaffolded. You can skip straight to *Deploy* below:

```bash
cd pipelines/hello-world && polyris-deploy
```

**If you installed manually (Option B):** install directly from the
checkout you made in Prerequisites:

```bash
cd ..   # repo root (if you're still in sam/)
pip install -e .
```

Or install a specific release from git without cloning the infrastructure repo:

```bash
pip install git+https://github.com/Polyris/polyris.git@v0.93.0
```

Replace `v0.93.0` with the latest tag from the [tags page](https://github.com/Polyris/polyris/tags).

### Configure config.py

**If you used the script (Option A):** `pipelines/config.py` already
exists — skip to *Deploy* below.

**If you installed manually (Option B):** create `pipelines/config.py`
(polyris walks up from each pipeline directory to find it):

```python
# pipelines/config.py
# The dict KEYS ("dev", "prod") are the stage names — that's what --stage picks.
ENVIRONMENTS = {
    "dev": {
        "stack_name": "myorg-dev",     # matches sam/samconfig.toml stack_name
        "namespace":  "myorg",
        "region":     "us-east-1",
        # "profile":    "my-aws-profile",  # optional
        # "account_id": "111111111111",    # optional — guards against wrong account
    },
    "prod": {
        "stack_name": "myorg-prod",
        "namespace":  "myorg",
        "region":     "us-east-1",
    },
}

DEFAULT_STAGE = "dev"
```

Or generate it automatically:
```bash
polyris-init --project
```

Read by `polyris-deploy` — it pulls wrapper ARN, role ARN, DDB tables from
the SAM stack named in `stack_name`. `ui/deploy.sh` does **not** read this
file; it takes stack / region / profile as CLI args (see Step 3 above).

See [CONFIGURATION.md](../reference/CONFIGURATION.md) for a per-field reference.

### Deploy

```bash
# Create a demo pipeline
cd pipelines
polyris-init hello-world
cd hello-world

# Edit dag.py — set a real task ARN, or use the test SFN from outputs:
# TEST_QUICK_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
#   --region "$AWS_REGION" --profile "$AWS_PROFILE" \
#   --query "Stacks[0].Outputs[?OutputKey=='TestQuickSfnArn'].OutputValue" --output text)

polyris-deploy --stage dev
```

Open the Console → see `hello-world` in the pipeline list → click **Run**.

---

## Multiple Environments

Each environment is a separate CloudFormation stack with its own SAM config file.

```bash
# dev — already done above

# prod
cp samconfig.toml samconfig.prod.toml
# Edit samconfig.prod.toml — change stack_name, profile, Stage=prod, and
# whatever else differs (alert channel, log level, …).

sam build
sam deploy --config-file samconfig.prod.toml
# sam reads stack_name / region / profile from the config-file, no need to
# repeat them on the CLI.
```

Add a matching entry under `ENVIRONMENTS["prod"]` in `config.py` (with the
prod stack name, profile, and roles). Pipelines then target the environment
by stage key:

```bash
polyris-deploy --stage prod
```

---

## Clean Up

If you installed with the script, tear down with:

```bash
./scripts/setup-polyris.sh --delete
```

Manual path:

```bash
# Remove pipelines first
cd pipelines/hello-world
polyris-destroy --stage dev

# Remove infrastructure (set STACK_NAME/AWS_REGION/AWS_PROFILE as in Step 1 if
# this is a fresh terminal).
cd sam
sam delete --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

`sam delete` empties `ConsoleUiBucket` and `ResultsBucket` automatically **only
when `AutoEmptyBucketsOnDelete=true`** is set in `samconfig.toml` (see Step
2.2). Without that flag it errors out on non-empty buckets and you have to
`aws s3 rm --recursive` them first.

---

## Next Steps

| I want to... | Go to |
|---|---|
| Write real pipelines | [TUTORIAL.md](TUTORIAL.md) |
| Learn the Python DSL | [DSL.md](../features/DSL.md) |
| Pass data between tasks (xcom) | [DATA_PASSING.md](../features/DATA_PASSING.md) |
| Asset-based orchestration | [ASSETS.md](../features/ASSETS.md) |
| SAM parameters reference | [SAM.md](../deployment/SAM.md) |
| Something's broken | [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md) |
