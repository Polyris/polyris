# Quickstart: install polyris in your AWS account

This guide takes you from a blank AWS account to a pipeline running in the polyris Console in about 15 minutes. You need Python and AWS credentials; nothing is built on your machine.

---

## What you need

- **Python 3.11 or newer.**
- **AWS credentials** for the account and region you install into, with rights to create IAM roles, Lambda functions, DynamoDB tables, Step Functions, S3 buckets, CloudFront and Cognito. Any standard source works: `aws configure`, environment variables, SSO, or AWS CloudShell. Exact permissions: [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md).

You don't need git, Node.js, the AWS SAM CLI or the AWS CLI. (If you sign in with AWS SSO, you use the AWS CLI for `aws sso login` only.)

> **Note:** AWS CloudShell is the zero-setup option: it already has your console credentials. Its built-in Python is 3.9, so install a newer one first: `sudo dnf install -y python3.11`. CloudShell keeps only your home directory between sessions, so after it restarts run that command again before using `polyris-*`.

---

## 1. Install the CLI

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
```

The installer finds Python 3.11+, installs the latest release into its own virtualenv (or with `pipx`, if you have it), and links the `polyris-*` commands into `~/.local/bin`. Pin a version with `POLYRIS_REF=<VERSION>` before `bash`.

Prefer pip? polyris isn't on PyPI — install a release's source archive straight from GitHub (no git needed):

```bash
python3 -m pip install https://github.com/Polyris/polyris/archive/refs/tags/<VERSION>.tar.gz
```

Replace `<VERSION>` with a tag from the [releases page](https://github.com/Polyris/polyris/releases), for example `v1.2.0`.

---

## 2. Install the platform

```bash
polyris-up
```

`polyris-up` installs the prebuilt release that matches your polyris version. It runs from any directory and writes nothing there. On a new install it asks for:

| Prompt | Meaning |
|---|---|
| Namespace | Prefix for every resource name. Bucket names are global, so pick something unique to you (your org). |
| Stage | `dev`, `prod`, … Default `dev`. Namespace + Stage may be at most 25 characters. |
| Admin email | Cognito creates this console user and emails a temporary password. Leave empty to add users later. |

It prints the AWS profile, region and account before it changes anything, then the console URL when it finishes (10–15 minutes on a new install):

```
── Polyris platform ────────────────────────────────────────
   profile:  (default chain)
   region:   eu-central-1
   account:  123456789012
   ...
✅ Polyris v1.2.0 is up: acme-dev
   Console:  https://d1234abcd.cloudfront.net
   Login:    you@acme.com — Cognito has emailed a temporary password.
```

Skip the prompts with flags (for CI): `polyris-up --namespace acme --stage dev --admin-email you@acme.com --yes`. Use `--profile` / `--region` to target another account or region; without them `polyris-up` uses the standard AWS credential chain and fails if no region is configured.

What gets created: [INFRASTRUCTURE.md](../deployment/INFRASTRUCTURE.md). How the install works: [ADR #131](../reference/adr-131-polyris-up.md).

---

## 3. Run your first pipeline

```bash
polyris-init hello-world --hello
cd hello-world
polyris-deploy
```

`--hello` finds your platform and scaffolds a pipeline that calls its built-in test Step Function (it succeeds after about a second), so the first deploy works without editing anything. Open the console, find `hello-world`, and press **Run**.

`polyris-deploy` finds the platform stack on its own while there is one platform in the region. With several (say `acme-dev` and `acme-prod`), create `pipelines/config.py` with `polyris-init --project` and set `stack_name` per stage — see [CONFIGURATION.md](../reference/CONFIGURATION.md).

Next, write a real pipeline: `polyris-init my-pipeline` scaffolds one with placeholder ARNs for your own Step Functions, Lambdas or Glue jobs. The DSL is in [DSL.md](../features/DSL.md).

---

## Add console users

`polyris-up --admin-email` creates the first admin. Add more in the AWS console (Cognito → user pool `<namespace>-<stage>-polyris-console` → **Create user**), or with the AWS CLI:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <CognitoUserPoolId output> \
  --username teammate@acme.com \
  --user-attributes Name=email,Value=teammate@acme.com Name=email_verified,Value=true
```

Cognito emails the temporary password. For API or CI access, see [authentication.md](../features/authentication.md).

---

## Change settings or upgrade

Change one template parameter and keep the rest:

```bash
polyris-up --param EnablePitr=true
```

Upgrade: install the newer polyris (re-run the installer), then run `polyris-up` again. It updates the stack in place with its current settings; running it again with nothing new reports "already up to date". With several stages in one region, name the one to upgrade: `polyris-up --stack acme-prod`. If a change would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user, `polyris-up` lists it and asks before continuing; in CI it stops unless you pass `--accept-data-loss` (`--yes` doesn't cover it).

> **Note:** `--admin-email` on an existing stack replaces the admin user — the previous one is deleted. Add extra users as described above instead.

### Already running polyris from `sam deploy`?

`polyris-up` doesn't find stacks created with `sam deploy` on its own (they carry no `polyris:platform` tag); for their namespace it stops and prints the command to use. Name the stack to switch it over:

```bash
polyris-up --stack myorg-dev
```

It keeps the stack's parameters, tags it, and from then on installs the UI itself (`UiBootstrap`) — `ui/deploy.sh` is no longer needed for that stack. If an install is interrupted before CloudFormation starts, running `polyris-up` again finishes it.

---

## Several stages

Run `polyris-up` once per stage:

```bash
polyris-up --namespace acme --stage prod --admin-email you@acme.com
```

Point pipelines at a stage with `polyris-deploy --stage prod` and a matching `ENVIRONMENTS["prod"]` entry in `pipelines/config.py` ([CONFIGURATION.md](../reference/CONFIGURATION.md)).

---

## Remove everything

```bash
cd hello-world && polyris-destroy      # each pipeline first
polyris-down                           # then the platform
```

`polyris-down` refuses while pipeline stacks still run on the platform, then lists what it deletes and asks you to type the stack name. The DynamoDB tables (run history, pipeline registry) are always deleted. If `ResultsBucket` still holds task results, it stops; re-run with `--delete-data` once you have what you need. It also deletes the artifacts bucket when no other platform is left in the region.

> **Warning:** deletion is permanent.

---

## Deploy from source (contributors)

To run polyris from a checkout — for example, to test a change to the template or a Lambda — use the SAM CLI and Node.js 20.19+:

```bash
git clone https://github.com/Polyris/polyris.git && cd polyris
pip install -e .

cd sam
cp samconfig.toml.example samconfig.toml   # set stack_name, Namespace, Stage, profile
sam build && sam deploy

cd ../ui
npm ci && npm run build
./deploy.sh <stack-name> <region> ./out --profile <profile>
```

`ui/deploy.sh` reads the stack outputs and writes `config.js`. A from-source stack isn't tagged as a polyris-up platform, so set `stack_name` in `pipelines/config.py` (or pass `--stack`) when you deploy pipelines to it.

To test the release path itself, build the archive CI publishes and install it with `polyris-up`:

```bash
(cd sam && sam build --build-dir ../build/sam)
(cd ui && npm ci && npm run build)
python scripts/build_platform_release.py --version 0.0.0-dev --sam-build-dir build/sam --ui-dir ui/out --out dist
polyris-up --archive dist/polyris-platform-v0.0.0-dev.zip --namespace mydev
```

---

## Next steps

| I want to... | Go to |
|---|---|
| Learn the Python DSL | [DSL.md](../features/DSL.md) |
| Pass data between tasks (xcom) | [DATA_PASSING.md](../features/DATA_PASSING.md) |
| Asset-based orchestration | [ASSETS.md](../features/ASSETS.md) |
| Stack resources + outputs reference | [INFRASTRUCTURE.md](../deployment/INFRASTRUCTURE.md) |
| Something's broken | [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md) |
