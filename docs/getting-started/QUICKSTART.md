# Quickstart: install polyris in your AWS account

From a blank AWS account to a pipeline running in the polyris Console, in about 15 minutes.

You need **Python 3.11+** and **AWS credentials** for the account you install into (any standard source: `aws configure`, SSO, environment variables). Required permissions: [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md).

## 1. Install the CLI

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
```

## 2. Install the platform

```bash
polyris-up
```

It asks for a **namespace** (a prefix unique to you, e.g. your org), a **stage** (default `dev`) and an **admin email**, then installs everything (10–15 minutes) and prints the console URL. Cognito emails the admin a temporary password.

## 3. Deploy a first pipeline

```bash
polyris-init hello-world --hello
cd hello-world
polyris-deploy
```

## 4. Run it

Open the console URL from step 2, sign in with the emailed password, find `hello-world`, and press **Run**. The task turns green in a few seconds.

## Next steps

| I want to... | Go to |
|---|---|
| Write a real pipeline | [DSL.md](../features/DSL.md) |
| Upgrade, change settings, add users or stages, remove polyris | [manage-the-platform.md](../how-to/manage-the-platform.md) |
| Install from AWS CloudShell | [manage-the-platform.md](../how-to/manage-the-platform.md#install-from-aws-cloudshell) |
| Run polyris from a checkout of this repo | [FROM_SOURCE.md](../deployment/FROM_SOURCE.md) |
| See what was created in my account | [INFRASTRUCTURE.md](../deployment/INFRASTRUCTURE.md) |
| Fix a problem | [TROUBLESHOOTING.md](../operations/TROUBLESHOOTING.md) |
