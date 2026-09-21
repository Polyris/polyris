# polyris

**Data pipelines that pause for you instead of failing on you.**

Serverless orchestration on AWS Step Functions. When a task fails, polyris
waits for a human decision (retry / mark success / skip / fail) instead of
nuking the run. Fix it inline, in the same execution.

```python
from polyris import DAG, task

with DAG("orders-etl", schedule="@daily") as dag:

    @task.glue_job(job_name="extract-orders")
    def extract(): pass

    @task.sfn(arn="arn:aws:states:us-east-1:...:transform", retries=2)
    def transform(): pass

    @task.lambda_function(function_name="load-orders")
    def load(): pass

    extract() >> transform() >> load()
```

`polyris-deploy` compiles this to a Step Functions state machine and hands
it to AWS. No scheduler to run. No metadata DB. Pay per run — a typical
multi-pipeline deployment lands at ~$31/month.

## Why polyris

**Like Dagster** — assets, cross-pipeline dependencies, lineage tracking.
**Unlike Dagster** — no cluster, no scheduler, no metadata DB. Pipelines
compile to Step Functions; AWS is the runtime.

- **Intervention-first failures.** Task fails → run pauses for a human
  decision (retry / mark success / skip / fail) instead of falling over.
  Fix inline, in the same run, no re-execution cost.
- **Nothing to run.** No scheduler, workers, or metadata DB. Step Functions
  scales to zero between runs.
- **Nothing hidden.** Every pipeline compiles to a state machine — each run
  is a debuggable execution history, not opaque scheduler state.
- **Asset-centric.** Pipelines declare first-class data assets with
  cross-pipeline dependencies, not just task graphs.
- **Pay per run.** Floor near zero because there is no always-on infra.

Details: `retries`, `trigger_rule`, seven task types (`sfn`, `lambda`,
`glue`, `ecs`, `athena`, `emr`, `batch`), asset schedules, `wait_for`
freshness checks, `xcom` data passing — all in
[docs/features/DSL.md](docs/features/DSL.md).

---

## Install

One command — checks prerequisites, clones the latest release, tells you
what to run next:

```bash
curl -fsSL https://raw.githubusercontent.com/Polyris/polyris/main/scripts/install.sh | bash
```

Pin a specific version with `POLYRIS_REF=v0.94.0` before the pipe, or use
`main` for bleeding edge. Full manual walkthrough:
[QUICKSTART.md](docs/getting-started/QUICKSTART.md).

---

## Try it locally (no AWS)

Validate DAGs, generate Step Functions JSON, mock-execute — all offline:

```bash
git clone https://github.com/Polyris/polyris
cd polyris
pip install -e .
polyris-init my-pipeline --local
cd my-pipeline

polyris-validate              # Validate structure
polyris-validate -v           # Verbose: tasks, deps, ASL preview
polyris-output --json         # Full Step Functions JSON
polyris-output --mermaid      # Mermaid diagram source
polyris-output --graph        # ASCII graph
```

Python API for automated tests:

```python
from polyris.local import validate, dry_run, run

validate(dag)                          # DAG structure
dry_run(dag)                           # Execution plan
result = run(dag, mock=True)           # Mock execution
print(result.summary())                # ✅ 3 succeeded, ❌ 0 failed
```

Or browse [examples/](examples/) — 15 self-contained pipelines from
hello-world through assets and lineage.

---

## Web Console

The install ships a React console served from CloudFront. Three primary
views (DAG, Tasks, Runs) with per-task actions on any failed or paused
task: **Skip**, **Mark Success**, **Fail**, **Stop**, **Restart**.

Full walkthrough: [docs/operations/UI.md](docs/operations/UI.md). Cognito
auth setup: [docs/features/authentication.md](docs/features/authentication.md).

---

## Documentation

| I want to... | Go to |
|---|---|
| **Set up polyris from a blank AWS account** | [QUICKSTART.md](docs/getting-started/QUICKSTART.md) |
| **Learn the Python DSL** — every task type, parameter, trigger rule | [DSL.md](docs/features/DSL.md) |
| **Pass data between tasks (xcom)** | [DATA_PASSING.md](docs/features/DATA_PASSING.md) |
| **Configure retries, backoff, jitter** | [how-to/configure-retries.md](docs/how-to/configure-retries.md) |
| **Set up asset-based orchestration** | [ASSETS.md](docs/features/ASSETS.md) |
| **Schedule a pipeline / pause / redeploy safely** | [how-to/schedule-and-redeploy.md](docs/how-to/schedule-and-redeploy.md) |
| **Set up Cognito authentication** | [authentication.md](docs/features/authentication.md) |
| **Test pipelines locally** | [LOCAL_TESTING.md](docs/tools/LOCAL_TESTING.md) |
| **Approve a polyris install** (IAM / resource inventory) | [IAM_PERMISSIONS.md](docs/deployment/IAM_PERMISSIONS.md) + [INFRASTRUCTURE.md](docs/deployment/INFRASTRUCTURE.md) |
| **See the REST API** | [API.md](docs/operations/API.md) |
| **Understand the runtime architecture** | [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) |
| **Read design decisions / ADRs** | [DESIGN_DECISIONS.md](docs/reference/DESIGN_DECISIONS.md) |
| **Troubleshoot a problem** | [TROUBLESHOOTING.md](docs/operations/TROUBLESHOOTING.md) |
| **Develop polyris itself** | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Project structure

```
├── pipelines/                    # Pipeline definitions (gitignored)
│   ├── config.py                 # Shared config: ENVIRONMENTS, DEFAULT_STAGE
│   └── my-pipeline/
│       └── dag.py                # Pipeline definition
│
├── sam/                          # Shared infrastructure (SAM/CloudFormation)
│   ├── template.yaml             # SAM template — all AWS resources
│   ├── samconfig.toml.example    # Deploy configuration template
│   ├── lambdas/                  # 8 Lambda functions
│   └── sfn_templates/            # 14 SFN definitions (11 orchestration + 3 test)
│
├── polyris/                      # Python DSL library
│   ├── dag.py                    # DAG class
│   ├── task.py                   # Task decorators
│   ├── assets.py                 # Asset definitions
│   └── generators.py             # ASL JSON generation
│
├── ui/                           # Web Console (React 19 + Next.js 16)
└── tests/                        # Test suite
```

---

## Cost

|  | Managed orchestrators | polyris |
|---|---|---|
| **Base cost** | ~$300+/month | $0 |
| **Per pipeline run** | $0 (included) | ~$0.01 |
| **8 tasks, 1×/day, 30 days** | ~$300+ | ~$0.50 |
| **Scaling** | Manual | Automatic |

---

## Requirements

- Python 3.11+
- AWS account

```bash
pip install -e .          # pipeline development
pip install -e ".[dev]"   # polyris development (adds pytest, ruff, mypy)
```

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and
PR guidelines.

```bash
make test    # Run all tests
make check   # Lint + sync + test (before PR)
```

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
