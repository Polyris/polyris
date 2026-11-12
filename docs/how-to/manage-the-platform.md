# Manage the polyris platform

How to upgrade polyris, change its settings, add users and stages, and remove it. Assumes you installed it with `polyris-up` ([QUICKSTART.md](../getting-started/QUICKSTART.md)). All options: [CLI.md](../reference/CLI.md#polyris-up).

## Upgrade

Install the newer polyris, then run `polyris-up` again:

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
polyris-up
```

It updates the stack in place and keeps its settings. With nothing new it reports "already up to date". An older polyris won't roll back a newer platform unless you pass `--allow-downgrade`.

If a change would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user, `polyris-up` lists it and asks before continuing. Without a terminal (CI) it stops unless you pass `--accept-data-loss`; `--yes` never covers data loss.

## Change a setting

Set one template parameter and keep the rest:

```bash
polyris-up --param EnablePitr=true
```

`Namespace` and `Stage` can't be changed this way — they name every resource.

## Add console users

The first admin comes from `polyris-up --admin-email`. Add more in the AWS console (Cognito → user pool `<namespace>-<stage>-polyris-console` → **Create user**), or with the AWS CLI:

```bash
aws cognito-idp admin-create-user \
  --user-pool-id <CognitoUserPoolId stack output> \
  --username teammate@acme.com \
  --user-attributes Name=email,Value=teammate@acme.com Name=email_verified,Value=true
```

Cognito emails the temporary password. API and CI access: [authentication.md](../features/authentication.md).

> **Note:** `polyris-up --admin-email` on an existing stack replaces the admin user — the previous one is deleted. Use the steps above for extra users.

## Add a stage

Run `polyris-up` once per stage:

```bash
polyris-up --namespace acme --stage prod --admin-email you@acme.com
```

With several platforms in one region, name the one you mean: `polyris-up --stack acme-prod`. For pipelines, create `pipelines/config.py` with `polyris-init --project` and set `stack_name` per stage ([CONFIGURATION.md](../reference/CONFIGURATION.md)); then `polyris-deploy --stage prod`.

## Install from AWS CloudShell

CloudShell already has your console credentials, so you need nothing on your machine. Its built-in Python is 3.9; install a newer one first:

```bash
sudo dnf install -y python3.11
```

CloudShell keeps only your home directory between sessions: after it restarts, run that command again before using `polyris-*`.

## Switch a stack created with `sam deploy`

`polyris-up` doesn't pick up stacks created with `sam deploy` on its own (they carry no `polyris:platform` tag); for their namespace it stops and prints the command to use:

```bash
polyris-up --stack myorg-dev
```

It keeps the stack's parameters, tags it, and from then on installs the UI itself — `ui/deploy.sh` is no longer needed for that stack.

## Recover an interrupted install

If an install stopped before CloudFormation started creating resources, run `polyris-up` again; it finishes the install. If the first create failed and rolled back, remove the stack with `polyris-down --stack <name>`, fix the cause shown in its events, and install again.

## Remove polyris

```bash
cd hello-world && polyris-destroy      # each pipeline first, from its directory
polyris-down                           # then the platform
```

`polyris-down` refuses while pipelines are still deployed on the platform, lists what it deletes, and asks you to type the stack name. The DynamoDB tables (run history, pipeline registry) are always deleted. If `ResultsBucket` still holds task results it stops; re-run with `--delete-data` once you have what you need. It also deletes the artifacts bucket when no other platform is left in the region.

> **Warning:** deletion is permanent.
