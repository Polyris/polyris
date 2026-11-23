# Manage the polyris platform

How to upgrade polyris, change its settings, add users and stages, and remove it. Assumes you installed it with `polyris-up` ([QUICKSTART.md](../getting-started/QUICKSTART.md)). All options: [CLI.md](../reference/CLI.md#polyris-up).

## Upgrade

Install the newer polyris, then run `polyris-up` again:

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
polyris-up
```

It updates the stack in place and keeps its settings. With nothing new it reports "already up to date". An older polyris won't roll back a newer platform unless you pass `--allow-downgrade`.

When the release notes ask for it, add `--disable-rollback`. A failed update then keeps what it already changed instead of rolling back; fix the cause its events name and run the same command again.

If a change would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user, `polyris-up` lists it and asks before continuing. Without a terminal (CI) it stops unless you pass `--accept-data-loss`; `--yes` never covers data loss.

## Upgrade from 1.2 to 1.3

1.3 adopts no run that 1.2 started, so this upgrade has steps of its own. A platform older than 1.2.0 goes to 1.2 first: install that release (`POLYRIS_REF=v1.2.3` with `install.sh`) and run its `polyris-up`.

1. Install polyris 1.3 and run `polyris-validate --all` in the folder that holds your pipelines' folders. Fix what it reports: a pipeline it refuses cannot be redeployed in step 5.
2. Grant the roles their new permissions: [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#upgrade-from-12-to-13).
3. Disable the pipelines' schedules (EventBridge Scheduler, the schedules named `…-schedule`), so that no new run starts. Then let the runs in flight finish, or stop them.
4. Run `polyris-up`.
5. Redeploy every pipeline with `polyris-deploy`. The redeploy turns the pipeline's schedule back on.

Before it changes anything, `polyris-up` checks the platform and refuses when:

- the platform is older than 1.2.0;
- 1.2 has not finished adopting the rows and facts of older versions, or finished less than 48 hours ago: until then they need code that 1.3 removes;
- a run is in flight.

Each refusal says what to do. To upgrade although a run is in flight or 1.2's adoption has not settled, pass `--accept-stopped-runs`. No flag upgrades a platform older than 1.2.0.

No run of 1.2 goes on after the upgrade. Once the update has succeeded, `polyris-up` stops what 1.2 left running and prints the runs it stopped: start those again after step 5. A run of a pipeline that you have redeployed since is kept.

After the update, `polyris-up` waits until DynamoDB has built the index that the console's run lists read (`run-start-index`): about 8 minutes on a table of 100,000 rows. You can interrupt that wait; the console says that the index is being built until it is. At the end it lists the pipelines that still need a redeploy, with the command for each. A later `polyris-up` lists them again, until none is left.

If the upgrade is interrupted, or `polyris-up` reports that it could not stop an execution, fix the cause and run `polyris-up` again. It stops what is left and goes on, also while CloudFormation still cleans up after the update.

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

## Track costs

Every resource in a polyris stack carries `polyris:` tags ([list](../deployment/INFRASTRUCTURE.md#tags)). To split your AWS bill by them:

1. Sign in to the account that pays the bill: the management account, if you use AWS Organizations.
2. In the Billing and Cost Management console, open **Cost allocation tags**, select the `polyris:` keys and choose **Activate**. A key is listed once a resource carries it, which can take AWS up to 24 hours.
3. In Cost Explorer, group or filter by a tag: `polyris:stage` compares your stages, `polyris:component` the platform with your pipelines.

Most orchestration cost is state transitions of the platform's shared state machines, so it counts as `platform`. `polyris:pipeline` covers a pipeline's own state machine, log group and scheduler role.

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
