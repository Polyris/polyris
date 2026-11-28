# polyris Documentation

Start with the [README](../README.md): what polyris is, how to install it, and [Try it locally](../README.md#try-it-locally-no-aws) without AWS. Then follow the [Quickstart](getting-started/QUICKSTART.md). Terms such as run, asset or stage: [GLOSSARY.md](../GLOSSARY.md).

## Getting started

| Document | What you get |
|----------|---------|
| [QUICKSTART.md](getting-started/QUICKSTART.md) | Install the platform and run a first pipeline in about 15 minutes |
| [PROJECT_STRUCTURE.md](getting-started/PROJECT_STRUCTURE.md) | How to lay out your pipelines in a repository, and CI/CD patterns |

## How-to guides

| Document | Question it answers |
|----------|---------------------|
| [configure-retries.md](how-to/configure-retries.md) | How do I set retries, backoff and jitter for a task? |
| [schedule-and-redeploy.md](how-to/schedule-and-redeploy.md) | When does the first run fire? How do I pause a schedule? What happens to running runs when I redeploy? |
| [manage-the-platform.md](how-to/manage-the-platform.md) | How do I upgrade polyris, change a setting, add users or a stage, or remove it? |
| [give-a-task-a-role-of-its-own.md](how-to/give-a-task-a-role-of-its-own.md) | How do I give one task rights the shared task role lacks, in the platform's account or another? |
| [LOCAL_TESTING.md](tools/LOCAL_TESTING.md) | How do I test a pipeline without AWS? |
| [REGISTRATION.md](features/REGISTRATION.md) | How do I register a pipeline by hand, or find why it is missing from the console? |

## Features

| Document | Description |
|----------|-------------|
| [DSL.md](features/DSL.md) | The Python DSL: `DAG`, task types, trigger rules |
| [DATA_PASSING.md](features/DATA_PASSING.md) | Passing data between tasks |
| [ASSETS.md](features/ASSETS.md) | Assets: schedule a pipeline on data, and `wait_for` |
| [notifications.md](features/notifications.md) | The console's bell: failures, decisions, warnings |
| [authentication.md](features/authentication.md) | Console sign-in and users |

## Operations

| Document | Description |
|----------|-------------|
| [UI.md](operations/UI.md) | The web console |
| [API.md](operations/API.md) | REST API reference |
| [TROUBLESHOOTING.md](operations/TROUBLESHOOTING.md) | Symptoms, checks and fixes |

## Reference

| Document | Description |
|----------|-------------|
| [CLI.md](reference/CLI.md) | Every command and its options |
| [CONFIGURATION.md](reference/CONFIGURATION.md) | `config.py`: stages, namespaces, roles |
| [GLOSSARY.md](../GLOSSARY.md) | The terms these docs use |

## Deployment

| Document | Description |
|----------|-------------|
| [INFRASTRUCTURE.md](deployment/INFRASTRUCTURE.md) | What the platform stack deploys: resources, outputs |
| [IAM_PERMISSIONS.md](deployment/IAM_PERMISSIONS.md) | Who holds which permissions to install, deploy, operate and remove; the deploy policy; what an author can reach |
| [CROSS_ACCOUNT_ROLES.md](deployment/CROSS_ACCOUNT_ROLES.md) | Run tasks in other AWS accounts |
| [FROM_SOURCE.md](deployment/FROM_SOURCE.md) | Deploy polyris from a checkout (contributors) |
| [RELEASE.md](deployment/RELEASE.md) | Release process and the platform archive `polyris-up` installs |

## Architecture

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](architecture/ARCHITECTURE.md) | How polyris works |
| [BACKEND.md](architecture/BACKEND.md) | The console API and its tables |

## Contributing

| Document | Description |
|----------|-------------|
| [CONTRIBUTING.md](../CONTRIBUTING.md) | Set up a checkout and send a change |
| [DEVELOPMENT.md](tools/DEVELOPMENT.md) | Tests, checks, CI, and how the console is built |
| [AGENTIC_WORKFLOW.md](tools/AGENTIC_WORKFLOW.md) | The coding-agent workflow and its guards |
| [Design decisions](decisions/README.md) | Why the code is the way it is: the ADRs in force |
| [TRADEMARK.md](reference/TRADEMARK.md) | Trademark and branding policy |
