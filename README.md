# polyris

**Data pipelines that pause for you instead of failing on you.**

Serverless orchestration on AWS Step Functions. When a task fails, polyris
waits for a human decision (retry / mark success / skip / fail) instead of
nuking the run. Fix it inline, in the same execution.

```python
from polyris import DAG, task

with DAG("orders-etl", schedule="@daily") as dag:

    @task.glue_job(job_name="extract-orders")
    def extract(): ...

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:transform",
              retries=2)
    def transform(): ...

    @task.lambda_function(function_name="load-orders")
    def load(): ...

    extract() >> transform() >> load()
```

`polyris-deploy` compiles this to a Step Functions state machine and hands
it to AWS. Pay per run — see [Cost](#cost) for the breakdown.

## Why polyris

Airflow, Dagster, and Prefect need a scheduler, workers, and a Postgres
metadata DB running 24/7. polyris compiles pipelines to Step Functions —
here is how the operations differ:

| | polyris | Airflow / Dagster / Prefect |
|---|---|---|
| **When a task fails** | Run pauses for a human decision — retry / mark success / skip / fail. Fix inline; upstream tasks that already succeeded don't re-run. | Retry policy, then the run fails. Re-execute from the failing task manually. |
| **Debugging a run** | Step Functions execution history in the AWS Console — graph, inputs, and error at each state. Familiar to anyone on-call for AWS. | Scheduler UI + per-task logs, one dashboard per orchestrator. |
| **Where state lives** | DynamoDB — pay per request, no schema to migrate, no cluster to administer. | Postgres you host, patch, and upgrade. |
| **Assets & lineage** | First-class: `outlets=` / `wait_for=` / asset-triggered schedules — same model as Dagster. | Native in Dagster; add-on or missing in Airflow / Prefect. |

Full DSL reference — `retries`, `trigger_rule`, seven task types (`sfn`,
`lambda`, `glue`, `ecs`, `athena`, `emr`, `batch`), asset schedules,
`wait_for` freshness checks, `xcom` data passing — in
[DSL.md](docs/features/DSL.md).

## Install

Requires **Python 3.11+** and an AWS account. One command — checks
prerequisites, clones the latest release, tells you what to run next:

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
```

Pin a specific version with `POLYRIS_REF=v0.94.0` before the pipe. Full
manual walkthrough (dev-mode setup, main-branch install, teardown):
[QUICKSTART.md](docs/getting-started/QUICKSTART.md).

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
from my_pipeline import dag                # your DAG object

validate(dag)                              # DAG structure
dry_run(dag)                               # Execution plan
result = run(dag, mock=True)               # Mock execution
print(result.summary())                    # ✅ 3 succeeded, ❌ 0 failed
```

Or browse [examples/](examples/) — 15 self-contained pipelines from
hello-world through assets and lineage.

## Documentation

| I want to... | Go to |
|---|---|
| Set up polyris from a blank AWS account | [QUICKSTART.md](docs/getting-started/QUICKSTART.md) |
| Learn the Python DSL — every task type, parameter, trigger rule | [DSL.md](docs/features/DSL.md) |
| Test pipelines locally (validate / dry_run / mock) | [LOCAL_TESTING.md](docs/tools/LOCAL_TESTING.md) |
| Pass data between tasks (xcom) | [DATA_PASSING.md](docs/features/DATA_PASSING.md) |
| Configure retries, backoff, jitter | [how-to/configure-retries.md](docs/how-to/configure-retries.md) |
| Schedule a pipeline, pause, redeploy safely | [how-to/schedule-and-redeploy.md](docs/how-to/schedule-and-redeploy.md) |
| Set up asset-based orchestration + `wait_for` | [ASSETS.md](docs/features/ASSETS.md) |
| Use the Web Console — DAG view, Runs, task actions | [UI.md](docs/operations/UI.md) |
| Set up Cognito authentication | [authentication.md](docs/features/authentication.md) |
| Talk to polyris over REST | [API.md](docs/operations/API.md) |
| Approve an install (IAM / resource inventory) | [IAM_PERMISSIONS.md](docs/deployment/IAM_PERMISSIONS.md) + [INFRASTRUCTURE.md](docs/deployment/INFRASTRUCTURE.md) |
| Fix a specific problem | [TROUBLESHOOTING.md](docs/operations/TROUBLESHOOTING.md) |
| Understand the runtime architecture | [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) |
| Read design decisions / ADRs | [DESIGN_DECISIONS.md](docs/reference/DESIGN_DECISIONS.md) |
| Develop polyris itself | [CONTRIBUTING.md](CONTRIBUTING.md) |

## Project structure

```
├── pipelines/                    # Your pipeline definitions (gitignored)
│   ├── config.py                 # Shared: ENVIRONMENTS, DEFAULT_STAGE
│   └── my-pipeline/
│       └── dag.py                # Pipeline definition
│
├── sam/                          # Shared AWS infrastructure
│   ├── template.yaml             # SAM template — all AWS resources
│   ├── lambdas/                  # 8 Lambda functions
│   └── sfn_templates/            # 14 SFN definitions (11 orchestration + 3 test)
│
├── polyris/                      # Python DSL library
│   ├── dag.py                    # DAG class
│   ├── task.py                   # Task decorators
│   ├── assets.py                 # Asset definitions
│   └── generators.py             # ASL JSON generation
│
├── ui/                           # Web Console
└── tests/                        # SDK + backend + docs tests
```

---

## Cost

polyris runs on Step Functions + Lambda + DynamoDB — all pay-per-request,
no fleet to keep alive:

| Component | AWS billing model | Approx. per-run share |
|---|---|---|
| Step Functions Standard | Per state transition ($0.025 / 1k) | $0.001 – $0.005 |
| Lambda (task orchestration) | Per invocation + duration | ~$0.0001 per task |
| DynamoDB | Per read/write unit | ~$0.001 per run |
| CloudWatch Logs | GB ingested | $0.50 – $2 / month total |

**Small deployment (10 pipelines, daily, ~10 tasks each) — ~$5–10/month
for orchestration.** Your task workloads (Glue jobs, Lambda functions, ECS
containers, Athena queries) bill separately against your existing AWS
spend; polyris does not add a percentage on top.

Compared to **Amazon MWAA** (managed Airflow) — the smallest `mw1.small`
environment alone starts at ~$350/month for the environment fee before
workers or metadata storage — polyris runs the same orchestration workload
for roughly two orders of magnitude less.

*Numbers are approximate AWS list prices as of late 2025 — check AWS
pricing pages for current figures.*

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and PR
guidelines. Inside a polyris checkout:

```bash
pip install -e ".[dev]"   # adds pytest, ruff, mypy
make test                 # Run all tests
make check                # Lint + sync + test (before PR)
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
