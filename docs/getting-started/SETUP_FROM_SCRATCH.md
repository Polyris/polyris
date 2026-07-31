# Complete Setup Guide (From Scratch)

Deploy polyris to a blank AWS account. ~30-45 minutes.

---

## Overview

```
1. Prerequisites (5 min)     — AWS CLI, SAM CLI, Node.js
2. Configure (5 min)         — samconfig.toml, Slack webhook
3. Deploy Infrastructure (10 min) — sam build && sam deploy
4. Deploy UI (5 min)         — npm build + ui/deploy.sh
5. Create first user (5 min) — Cognito (if auth enabled)
6. Deploy first pipeline (5 min) — polyris-deploy
```

---

## Step 1: Prerequisites

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

---

## Step 2: Configure

### 2.1 Clone the repo

```bash
git clone https://github.com/Polyris/polyris.git
cd polyris
```

### 2.2 Create samconfig.toml

```bash
cd sam
cp samconfig.toml.example samconfig.toml
```

Edit `samconfig.toml` — set the required values:

```toml
[default.deploy.parameters]
parameter_overrides = [
  "Namespace=myorg",         # prefix for all resource names: myorg-dev-polyris-*
  "Stage=dev",               # dev | prod
  "AwsRegion=us-east-1",
  "EnableCognitoAuth=true",  # set false to skip auth during initial testing
]
```

Everything else in `samconfig.toml` (PagerDuty, custom domain, log levels) is optional.

> **Your stack name lives in `samconfig.toml`** — the `stack_name` field (the
> example sets `polyris-dev`). The `sam` commands read it straight from there, so
> that file is the single source of truth. Two things **can't** read it, though:
> the UI deploy (`./deploy.sh`) and the AWS CLI output lookups (`describe-stacks`).
> Give them the same name or they fail with *"Stack … does not exist"*.

Set your values as shell variables **once in this terminal** so the commands below
stay copy-pasteable. Keep `STACK_NAME` equal to `stack_name` in `samconfig.toml`:

```bash
STACK_NAME=polyris-dev     # = stack_name in samconfig.toml
AWS_REGION=us-east-1       # = AwsRegion in samconfig.toml
AWS_PROFILE=polyris-dev    # the profile from `aws configure --profile …`
```

The UI deploy and the CLI lookups below take these explicitly, so nothing rides on
a hidden default. Opening a new terminal later? Set them again first.

### 2.3 Configure config.py

In the **pipelines repo root**, create `config.py`:

```python
# config.py
ENVIRONMENTS = {
    "dev": {
        "namespace": "myorg",
        "stage": "dev",
        "region": "us-east-1",
        # "profile": "my-aws-profile",  # optional
    },
    "prod": {
        "namespace": "myorg",
        "stage": "prod",
        "region": "us-east-1",
    },
}

DEFAULT_STAGE = "dev"
```

Or generate it automatically:
```bash
polyris-init --project
```

This is read by `polyris-deploy` when deploying pipelines.

### 2.4 Get a Slack webhook (if you don't have one)

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. **Incoming Webhooks** → toggle on → **Add New Webhook to Workspace**
3. Pick a channel (e.g. `#pipeline-alerts`) → **Allow**
4. Copy the webhook URL

Test it:
```bash
curl -X POST -H 'Content-type: application/json' \
  --data '{"text":"polyris test"}' \
  https://hooks.slack.com/services/YOUR/WEBHOOK/URL
```

---

## Step 3: Deploy Infrastructure

```bash
cd sam
sam build
sam deploy --profile "$AWS_PROFILE"
# sam reads the stack name, region, and parameters from samconfig.toml (the single
# source of truth). Pass only --profile — it's the one thing not stored there.
```

First deploy takes ~5-10 minutes. Creates:
- 8 DynamoDB tables
- 16 Step Functions state machines (13 templates + 3 test SFNs)
- 6 Lambda functions
- API Gateway, Cognito, S3, CloudFront

All outputs are written to SSM automatically:
```bash
# View stack outputs
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --query "Stacks[0].Outputs" \
  --output table \
  --profile "$AWS_PROFILE"
```

---

## Step 4: Deploy UI

```bash
cd ../ui
npm ci
npm run build
# Pass the stack name explicitly — it must match `stack_name` in samconfig.toml.
# (Omitting it makes deploy.sh fall back to a `polyris-dev` default, which breaks
# if you renamed the stack.)
./deploy.sh "$STACK_NAME" "$AWS_REGION" ./out --profile "$AWS_PROFILE"
```

The script reads CloudFormation outputs, generates `config.js` with the real API Gateway URL and Cognito settings, uploads to S3, and invalidates CloudFront.

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

## Step 5: Create First User (Cognito)

Skip if you set `EnableCognitoAuth=false`.

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

> **For API/CLI/CI access** (not the browser), generate a Personal Access Token
> after logging in: avatar → **API Tokens** → Generate. Use it as
> `Authorization: Bearer plrs_…`. See
> [api-tokens.md](../features/api-tokens.md). (Auth is enforced only when
> `AUTH_ENABLED=true`.)

---

## Step 6: Deploy First Pipeline

```bash
cd ..   # repo root
pip install -e .

# Create a demo pipeline
cd pipelines
polyris-init hello-world
cd hello-world

# Edit dag.py — set a real task ARN, or use the test SFN from outputs:
# TEST_QUICK_ARN=$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
#   --region "$AWS_REGION" --profile "$AWS_PROFILE" \
#   --query "Stacks[0].Outputs[?OutputKey=='TestQuickSfnArn'].OutputValue" --output text)

polyris-deploy --stage dev --profile "$AWS_PROFILE"
```

Open the Console → see `hello-world` in the pipeline list → click **Run**.

---

## Multiple Environments

Each environment is a separate CloudFormation stack with its own `samconfig.toml`:

```bash
# dev — already done above

# prod
cp samconfig.toml samconfig.prod.toml
# Edit samconfig.prod.toml: Stage=prod, Namespace=myorg, different Slack channel, etc.

# Prod is a SEPARATE stack — give it its own name/profile (don't reuse the dev vars).
sam build
sam deploy --config-file samconfig.prod.toml \
  --stack-name polyris-prod --region us-east-1 --profile polyris-prod
```

Pipelines target environments via `--stage`:
```bash
polyris-deploy --stage prod --profile polyris-prod   # prod profile, not the dev one
```

---

## Clean Up

```bash
# Remove pipelines first
cd pipelines/hello-world
polyris-deploy --destroy --stage dev

# Remove infrastructure (set STACK_NAME/AWS_REGION/AWS_PROFILE as in Step 2.2 if
# this is a fresh terminal).
cd sam
sam delete --stack-name "$STACK_NAME" --region "$AWS_REGION" --profile "$AWS_PROFILE"
```

---

## Next Steps

| I want to... | Go to |
|---|---|
| Write real pipelines | [TUTORIAL.md](TUTORIAL.md) |
| Learn the Python DSL | [DSL.md](../features/DSL.md) |
| Asset-based orchestration | [ASSETS.md](../features/ASSETS.md) |
| SAM parameters reference | [SAM.md](../deployment/SAM.md) |
| Something's broken | [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md) |
