# Project structure

How to lay out a repository of pipelines, keep one `config.py` for your stages, and validate and deploy from CI. Assumes the platform is installed ([QUICKSTART.md](QUICKSTART.md)).

## Install

For the CLI on your machine, use install.sh ([README → Install](../../README.md#install)). For CI, or to import polyris in your own Python environment:

```bash
pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
```

Use the version your platform runs.

## Layout

One folder per pipeline, each with a `dag.py`, and one `config.py` next to them:

```
data-pipelines/
├── pipelines/
│   ├── config.py               # your stages: platform stack, namespace, region, account
│   ├── orders-daily/
│   │   └── dag.py
│   └── clicks-hourly/
│       └── dag.py
└── .github/
    └── workflows/
        └── pipelines.yml
```

`polyris-init --project` writes `pipelines/config.py`. Create each pipeline inside `pipelines/`, so the commands find `config.py` from the pipeline's folder:

```bash
polyris-init --project
cd pipelines
polyris-init orders-daily
cd orders-daily && polyris-deploy
```

`config.py` holds no secret. Commit it, so CI deploys with the same settings, and leave `profile` out of it: CI has no named profiles, so set `AWS_PROFILE` on your machine instead. Every field: [CONFIGURATION.md](../reference/CONFIGURATION.md).

When one platform team serves several data teams, each team keeps a repository of this shape; the platform team installs the platform once with `polyris-up`.

## A pipeline file

A `dag.py` can pick its resources by stage. `polyris-deploy --stage prod` sets `POLYRIS_STAGE=prod` before it runs the file:

```python
import os

from polyris import DAG, task

STAGE = os.environ.get("POLYRIS_STAGE", "dev")

with DAG(dag_id="orders-daily", schedule="@daily") as dag:

    @task.sfn(arn=f"arn:aws:states:us-east-1:123456789012:stateMachine:acme-{STAGE}-extract-orders")
    def extract():
        pass

    @task.sfn(arn=f"arn:aws:states:us-east-1:123456789012:stateMachine:acme-{STAGE}-load-orders")
    def load():
        pass

    extract() >> load()
```

## Stages

Add a stage to `ENVIRONMENTS` in `config.py` for each platform you installed (`polyris-up --stage prod` creates the stack `<namespace>-prod`):

```python
# pipelines/config.py
ENVIRONMENTS = {
    "dev": {"stack_name": "acme-dev", "namespace": "acme", "region": "us-east-1"},
    "prod": {"stack_name": "acme-prod", "namespace": "acme", "region": "us-east-1"},
}
DEFAULT_STAGE = "dev"
```

Pick the stage with `--stage` or `POLYRIS_STAGE`; without either, `DEFAULT_STAGE` applies:

```bash
polyris-deploy                  # dev
polyris-deploy --stage prod     # prod
```

## CI with GitHub Actions

Validate every pipeline on each push, and deploy them all from `main`. The deploy job assumes an AWS role through GitHub's OIDC provider, so no AWS keys are stored in GitHub; the role needs the deploy permissions of [IAM_PERMISSIONS.md](../deployment/IAM_PERMISSIONS.md#deploy--per-pipeline-polyris-deploy).

```yaml
# .github/workflows/pipelines.yml
name: pipelines

on:
  push:
    branches: [main]
  pull_request:

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
      - name: Validate every pipeline
        working-directory: pipelines
        run: polyris-validate --all

  deploy-dev:
    needs: validate
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      id-token: write       # lets the job assume the AWS role below
      contents: read
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: arn:aws:iam::123456789012:role/acme-pipelines-deploy
          aws-region: us-east-1
      - name: Deploy every pipeline
        working-directory: pipelines
        run: polyris-deploy --all --stage dev
```

`polyris-validate --all` and `polyris-deploy --all` act on every folder of the current directory and exit 1 if any pipeline fails.
