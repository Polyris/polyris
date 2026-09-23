# polyris Documentation

## Getting Started

| Document | Purpose | Reader |
|----------|---------|--------|
| [QUICKSTART.md](getting-started/QUICKSTART.md) | **How-to** — deploy polyris to a blank AWS account in ~10-15 min | I know what polyris does; show me how to install it |
| [PROJECT_STRUCTURE.md](getting-started/PROJECT_STRUCTURE.md) | **Reference** — monorepo vs split-repo layouts, CI/CD patterns | I'm setting up a real project; show me the shape |

To explore the DSL locally without AWS, follow the "Try It Now" section in
the [root README](../README.md#try-it-locally-no-aws) — install, `polyris-init`,
inspect the ASL / graph / Mermaid output.

## How-to Guides

Task-oriented recipes for specific problems. Each answers one question.

| Document | Question it answers |
|----------|---------------------|
| [configure-retries.md](how-to/configure-retries.md) | How do I set retries + backoff + jitter for a task, and when do I use each? |
| [schedule-and-redeploy.md](how-to/schedule-and-redeploy.md) | When does the first run fire? How do I pause a schedule? What happens to in-flight runs when I redeploy? |
| [LOCAL_TESTING.md](tools/LOCAL_TESTING.md) | How do I test a pipeline without deploying to AWS? |
| [REGISTRATION.md](features/REGISTRATION.md) | How do I manually register a pipeline (or diagnose why it's missing from the Console)? |

## Features

| Document | Description |
|----------|-------------|
| [DSL.md](features/DSL.md) | Python DSL reference |
| [ASSETS.md](features/ASSETS.md) | Asset-based orchestration (including `wait_for` pull-based dependencies) |
| [authentication.md](features/authentication.md) | Cognito authentication setup |

## Tools

| Document | Description |
|----------|-------------|
| [LOCAL_TESTING.md](tools/LOCAL_TESTING.md) | validate, dry_run, mock execution |
| [DEVELOPMENT.md](tools/DEVELOPMENT.md) | Dev scripts, testing, code quality |

## Deployment

| Document | Description |
|----------|-------------|
| [DEPLOY.md](deployment/DEPLOY.md) | Pipeline deployment (polyris-deploy) |
| [INFRASTRUCTURE.md](deployment/INFRASTRUCTURE.md) | What the polyris SAM stack deploys (resource inventory, stack outputs, SFN template sources) |
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

## Reference

| Document | Description |
|----------|-------------|
| [CLI.md](reference/CLI.md) | Complete CLI reference — all commands and options |
| [CONFIGURATION.md](reference/CONFIGURATION.md) | config.py settings, environments, cross-account roles |
| [DESIGN_DECISIONS.md](reference/DESIGN_DECISIONS.md) | Key design decisions |
| [STATE.md](reference/STATE.md) | Current state — in-flight, decided-but-not-done |
| [TRADEMARK.md](reference/TRADEMARK.md) | Trademark and branding policy |
