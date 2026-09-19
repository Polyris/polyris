# polyris Documentation

## Getting Started

| Document | Purpose | Reader |
|----------|---------|--------|
| [TUTORIAL.md](getting-started/TUTORIAL.md) | **Tutorial** — explore the DSL locally in ~10 min, no AWS needed | I've never used polyris; teach me by doing |
| [QUICKSTART.md](getting-started/QUICKSTART.md) | **How-to** — deploy polyris to a blank AWS account in ~10-15 min | I know what polyris does; show me how to install it |
| [PROJECT_STRUCTURE.md](getting-started/PROJECT_STRUCTURE.md) | **Reference** — monorepo vs split-repo layouts, CI/CD patterns | I'm setting up a real project; show me the shape |

## How-to Guides

Task-oriented recipes for specific problems. Each answers one question.

| Document | Question it answers |
|----------|---------------------|
| [configure-retries.md](how-to/configure-retries.md) | How do I set retries + backoff + jitter for a task, and when do I use each? |
| [schedule-and-redeploy.md](how-to/schedule-and-redeploy.md) | When does the first run fire? How do I pause a schedule? What happens to in-flight runs when I redeploy? |
| [LOCAL_TESTING.md](tools/LOCAL_TESTING.md) | How do I test a pipeline without deploying to AWS? |
| [REGISTRATION.md](tools/REGISTRATION.md) | How do I manually register a pipeline (or diagnose why it's missing from the Console)? |

## Features

| Document | Description |
|----------|-------------|
| [DSL.md](features/DSL.md) | Python DSL reference |
| [ASSETS.md](features/ASSETS.md) | Asset-based orchestration |
| [ASSET_PULL_FEATURE.md](features/ASSET_PULL_FEATURE.md) | wait_for / pull-based assets |
| [authentication.md](features/authentication.md) | Cognito authentication setup |

## Tools

| Document | Description |
|----------|-------------|
| [LOCAL_TESTING.md](tools/LOCAL_TESTING.md) | validate, dry_run, mock execution |
| [REGISTRATION.md](tools/REGISTRATION.md) | Pipeline registration CLI |
| [DEVELOPMENT.md](tools/DEVELOPMENT.md) | Dev scripts, testing, code quality |

## Deployment

| Document | Description |
|----------|-------------|
| [DEPLOY.md](deployment/DEPLOY.md) | Pipeline deployment (polyris-deploy) |
| [SAM.md](deployment/SAM.md) | SAM infrastructure deployment and parameters |
| [INFRASTRUCTURE.md](deployment/INFRASTRUCTURE.md) | What the polyris SAM stack deploys (resource inventory + purpose per resource) |
| [IAM_PERMISSIONS.md](deployment/IAM_PERMISSIONS.md) | Minimum permissions for install / deploy / operate / remove |
| [RELEASE.md](deployment/RELEASE.md) | Release process and Launch Stack |
| [CROSS_ACCOUNT_ROLES.md](deployment/CROSS_ACCOUNT_ROLES.md) | Multi-account IAM setup |

## Operations

| Document | Description |
|----------|-------------|
| [API.md](operations/API.md) | REST API reference (27 free endpoints; 63 in the full build) |
| [UI.md](operations/UI.md) | Web Console guide |
| [TROUBLESHOOTING.md](operations/TROUBLESHOOTING.md) | Common issues and solutions |

## Architecture

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](architecture/ARCHITECTURE.md) | System architecture |
| [BACKEND.md](architecture/BACKEND.md) | Backend implementation |
| [STEP_FUNCTIONS.md](architecture/STEP_FUNCTIONS.md) | ASL patterns and helpers |

## Reference

| Document | Description |
|----------|-------------|
| [CLI.md](reference/CLI.md) | Complete CLI reference — all commands and options |
| [CONFIGURATION.md](reference/CONFIGURATION.md) | config.py settings, environments, cross-account roles |
| [DESIGN_DECISIONS.md](reference/DESIGN_DECISIONS.md) | Key design decisions |
| [STATE.md](reference/STATE.md) | Current state — in-flight, decided-but-not-done |
| [TRADEMARK.md](reference/TRADEMARK.md) | Trademark and branding policy |
