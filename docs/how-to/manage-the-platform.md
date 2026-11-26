# Manage the polyris platform

How to upgrade polyris, change its settings, add users and stages, and remove it. Assumes you installed it with `polyris-up` ([QUICKSTART.md](../getting-started/QUICKSTART.md)). All options: [CLI.md](../reference/CLI.md#polyris-up).

## Upgrade

Install the newer polyris, then run `polyris-up` again:

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
polyris-up
```

It updates the stack in place and keeps its settings. Before it uploads or changes anything, it runs its checks ([what they may refuse](#what-the-checks-may-refuse)). With nothing new it reports "already up to date". A failed update rolls itself back. An older polyris won't roll back a newer platform unless you pass `--allow-downgrade`, and nothing takes a platform on 1.4 or newer below 1.4 (see [below](#no-way-back-below-14)).

When the release notes ask for it, add `--disable-rollback`. A failed update then keeps what it already changed instead of rolling back; fix the cause its events name and run the same command again.

If a change would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user, `polyris-up` lists it and asks before continuing. Without a terminal (CI) it stops unless you pass `--accept-data-loss`; `--yes` never covers data loss.

From 1.2: upgrade to 1.3 first, with the `polyris-up` of 1.3: follow [upgrade-1.2-to-1.3.md](upgrade-1.2-to-1.3.md). From 1.3: follow [Upgrade to 1.4.0](#upgrade-to-140).

## Upgrade to 1.4.0

1.4.0 splits the work between two people. The infrastructure engineer installs and updates the platform with `polyris-up` and is the only one who creates IAM roles. Pipeline authors deploy with `polyris-deploy` under a managed policy that the platform now creates, `<namespace>-<stage>-polyris-deploy-policy`, which holds no IAM write. Every scheduled pipeline starts with one schedule role of the platform instead of a role of its own. Do the steps in this order: a 1.3 `polyris-deploy` under the new policy can't create a scheduler role, and a 1.4 `polyris-deploy` can't schedule a pipeline on a 1.3 platform.

1. **Update the platform.** Install polyris 1.4.0 and run `polyris-up`. Its checks run first; fix what they list ([below](#what-the-checks-may-refuse)) and run it again.

   ```bash
   curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | POLYRIS_REF=v1.4.0 bash
   polyris-up
   ```

   At its end it prints the deploy policy, and the order of the next steps:

   ```text
   ⚠ Pipeline authors need the new deploy policy: attach acme-dev-polyris-deploy-policy (arn:aws:iam::123456789012:policy/acme-dev-polyris-deploy-policy) to their role or CI role and detach the old one.
   ```

2. **Attach the deploy policy** to every pipeline author's role and to each CI role that deploys pipelines. Keep the policy they hold now attached for the moment, so that no `polyris-deploy` of 1.3 breaks:

   ```bash
   aws iam attach-role-policy --role-name pipeline-deployer --policy-arn arn:aws:iam::123456789012:policy/acme-dev-polyris-deploy-policy
   ```

3. **Move the authors to polyris 1.4.0:** each author installs it as in step 1, and each CI job pins it.
4. **Detach the old policy**, the one written for earlier releases, then run `polyris-up` once more: its checks look again for a scheduler role planted meanwhile.

The pipelines keep running all along. Redeploy them at your pace: each scheduled pipeline moves to the platform's schedule role at its next `polyris-deploy`, which deletes the pipeline's own scheduler role. A task whose `role=` names a role of the platform's account outside the path `/polyris-tasks/` stops the update until it is fixed ([below](#what-the-checks-may-refuse)).

### What the checks may refuse

`polyris-up` stops before it changes anything, prints what to fix, and ends with "Then run polyris-up again".

| It says | Fix |
|---|---|
| `… runs v1.2.x, and polyris 1.4.0 upgrades a platform from 1.3 only.` | Upgrade to 1.3 first: install polyris 1.3, run its `polyris-up` ([upgrade-1.2-to-1.3.md](upgrade-1.2-to-1.3.md)), then run this one |
| `N task(s) of deployed pipelines run as a role of this account outside /polyris-tasks/, which polyris 1.4.0 refuses:` and a line per task | IAM can't move a role to another path. Create each role again under `/polyris-tasks/` with the same trust and permissions (`polyris-up` prints the `aws iam create-role` command), point the task's `role=` at the new ARN, and redeploy the pipeline ([give-a-pipeline-its-own-permissions.md](give-a-pipeline-its-own-permissions.md)) |
| `N role(s) named like a pipeline's scheduler role are not what polyris-deploy made:` and each role with what is wrong | 1.4's deploy policy may pass and delete the roles named `*-polyris-*-scheduler-role`, so the update waits until each one is the role `polyris-deploy` made. Review each: CloudTrail's `CreateRole`, `PutRolePolicy`, `AttachRolePolicy` and `UpdateAssumeRolePolicy` events name who made or changed it. Delete a role that is not a pipeline's |
| `polyris-up may not call <action> on <resource>, which its checks before any change need.` | Grant the action to the role that runs `polyris-up` ([IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#the-checks-before-any-change)) |
| `Could not read the task specs of pipeline <name>: …` | Redeploy that pipeline |

### No way back below 1.4

Once a platform runs 1.4 or newer, no `polyris-up` installs a release below 1.4 over it, with or without `--allow-downgrade`: its scheduled pipelines start with the platform's schedule role, which an older release does not have. CloudFormation holds the same line for an older `polyris-up`: every scheduled pipeline's stack imports the role, and CloudFormation keeps an export while a stack imports it. A failed update to 1.4 rolls itself back to 1.3 as any failed update does.

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

With several platforms in one region, name the one you mean: `polyris-up --stack acme-prod`. A new platform's stack name may not contain `-polyris-`, the form of a pipeline's stack. For pipelines, add the stage to `pipelines/config.py` ([PROJECT_STRUCTURE.md](../getting-started/PROJECT_STRUCTURE.md#stages)); then `polyris-deploy --stage prod`.

## Track costs

Every resource in a polyris stack carries `polyris:` tags ([list](../deployment/INFRASTRUCTURE.md#tags)). To split your AWS bill by them:

1. Sign in to the account that pays the bill: the management account, if you use AWS Organizations.
2. In the Billing and Cost Management console, open **Cost allocation tags**, select the `polyris:` keys and choose **Activate**. A key is listed once a resource carries it, which can take AWS up to 24 hours.
3. In Cost Explorer, group or filter by a tag: `polyris:stage` compares your stages, `polyris:component` the platform with your pipelines.

Most orchestration cost is state transitions of the platform's shared state machines, so it counts as `platform`. `polyris:pipeline` covers a pipeline's own state machine and log group.

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

Destroy the pipelines first: `polyris-down` refuses while pipelines are still deployed on the platform, and CloudFormation can't delete the platform's schedule role while a scheduled pipeline's stack imports it. Then `polyris-down` lists what it deletes and asks you to type the stack name. The DynamoDB tables (run history, pipeline registry) are always deleted. If `ResultsBucket` still holds task results it stops; re-run with `--delete-data` once you have what you need. It also deletes the artifacts bucket when no other platform is left in the region.

> **Warning:** deletion is permanent.
