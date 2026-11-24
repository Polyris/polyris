# Project Structure

This guide covers different ways to organize polyris projects.

## Installation

```bash
# Replace <VERSION> with your platform's version, a tag from
# https://github.com/Polyris/polyris/tags (e.g. v1.3.0).
pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"
```

## Structure Options

### Option A: Single Repository (Simple)

Best for: Small teams, getting started, monorepo setups.

```
mycompany-data/
├── pyproject.toml              # Python packaging + dev tools
├── pipelines/
│   ├── config.py               # gitignored: real account_id / profile stay out of git
│   ├── acme-daily/
│   │   └── dag.py
│   └── nexus-hourly/
│       └── dag.py
│
└── .github/
    └── workflows/
        └── ci.yml
```

**pipelines/config.py**:
```python
# pipelines/config.py
ENVIRONMENTS = {
    "dev": {
        "stack_name": "mycompany-dev",
        "namespace": "mycompany",
        "region": "us-east-1",
    },
}

DEFAULT_STAGE = "dev"
```

**Workflow:**
```bash
# 1. Install the platform (one-time)
polyris-up --namespace mycompany --stage dev

# 2. Create pipeline
cd pipelines
polyris-init my-pipeline
cd my-pipeline

# 3. Deploy
polyris-deploy
```

---

### Option B: Separate Repositories (Production)

Best for: Multiple teams, separate ownership.

The platform team installs the platform with `polyris-up`; each data team keeps its pipelines in a repository of its own:

```
data-pipelines/
├── pyproject.toml              # Python packaging
├── pipelines/
│   ├── config.py               # gitignored: carries real account_id / profile
│   ├── acme-daily/
│   └── nexus-hourly/
└── .github/workflows/ci.yml
```

---

## Pipeline File Structure

Each pipeline needs a single `dag.py`:

```
my-pipeline/
└── dag.py     # Pipeline definition
```

**dag.py:**
```python
from polyris import DAG, task
import os

STAGE = os.environ.get("POLYRIS_STAGE", "dev")

# Config from config.py ENVIRONMENTS

with DAG(
    dag_id="my-pipeline",
    schedule="@daily",
) as dag:
    
    @task.sfn(arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-extract-task")
    def extract(): pass
    
    @task.sfn(arn=f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-load-task")
    def load(): pass
    
    extract() >> load()

# Deploy: POLYRIS_STAGE=prod polyris-deploy
```

---

## CI/CD

### GitHub Actions

```yaml
# .github/workflows/ci.yml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      
      - name: Install
        run: pip install -e ".[dev]"
      
      - name: Lint
        run: ruff check .
      
      - name: Test
        run: pytest tests/

  deploy-dev:
    needs: test
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    env:
      POLYRIS_STAGE: dev
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: pip install -e .
      - name: Deploy pipeline
        run: |
          cd pipelines/my-pipeline
          polyris-deploy --stage dev
```

---

## Environment Management

### Via pipelines/config.py + Environment Variables

`pipelines/config.py` defines per-stage settings:

```python
# pipelines/config.py
ENVIRONMENTS = {
    "dev": {"stack_name": "mycompany-dev", "namespace": "mycompany", "region": "us-east-1"},
    "prod": {"stack_name": "mycompany-prod", "namespace": "mycompany", "region": "us-east-1"},
}
DEFAULT_STAGE = "dev"
```

Override in CI or command line:
```bash
# Deploy to dev (default)
polyris-deploy

# Deploy to prod
polyris-deploy --stage prod

# Or via environment variable
export POLYRIS_STAGE=prod
polyris-deploy
```

In code:
```python
import os
STAGE = os.environ.get("POLYRIS_STAGE", "dev")
f"arn:aws:states:us-east-1:ACCOUNT_ID:stateMachine:myorg-{STAGE}-task"
```

> **Note:** `--stage` does not reach `dag.py`: it picks the stage's config, while the code above reads `POLYRIS_STAGE`. When a pipeline's code reads the stage, set `POLYRIS_STAGE` instead of passing `--stage`.

---

## Best Practices

1. **One `pipelines/config.py`** with all polyris ENVIRONMENTS config
2. **One pipeline per folder** with a `dag.py`
3. **Use full ARN strings directly** — explicit and transparent
4. **Use environment variables** for stage/account overrides in CI
5. **Test locally** before deploying: `polyris-validate -v`

---

## Next Steps

- [CONFIGURATION.md](../reference/CONFIGURATION.md) — All config options
- [DSL.md](../features/DSL.md) — Pipeline DSL reference
