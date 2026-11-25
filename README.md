# polyris

**Data pipelines that pause for you instead of failing on you.**

For teams on AWS who want scheduled data pipelines without running Airflow.

You write a pipeline in Python. Each task's work runs in AWS Step Functions, which calls your Glue jobs, Lambda functions, containers or queries; there is no server to keep running, and everything stays in your AWS account. When a task fails, the run waits for your decision — retry, mark success, skip or fail — and goes on from there, in the same execution.

```python
from polyris import DAG, task

with DAG("orders-etl", schedule="@daily") as dag:

    @task.glue_job(job_name="extract-orders")   # runs your existing Glue job
    def extract(): ...                          # the body doesn't run in AWS

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:transform",
              retries=2)
    def transform(): ...

    @task.lambda_function(function_name="load-orders")
    def load(): ...

    extract() >> transform() >> load()
```

`polyris-deploy` turns the file into a Step Functions state machine in your account, with an EventBridge schedule if the pipeline has one. In each run, the polyris dispatcher, a Lambda function, starts each task as soon as its upstream tasks and the data it waits for allow it; Step Functions runs the task's work, with its retries and timeout. How it works: [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md). The words polyris uses: [GLOSSARY.md](GLOSSARY.md). Pay per run — see [Cost](#cost).

## Why polyris

Airflow, Dagster, and Prefect need a scheduler, workers, and a Postgres metadata DB running 24/7. polyris has no server: a Lambda function decides when each task starts, and Step Functions runs every task's work. Here is how the operations differ:

| | polyris | Airflow / Dagster / Prefect |
|---|---|---|
| **When a task fails** | The run waits for your decision — retry, mark success, skip, fail — then continues in the same execution. Upstream tasks that already succeeded don't re-run. | Retry policy; then you clear the task and re-run it from the UI or CLI. |
| **Debugging a run** | The polyris console shows each task's status, why it waits and its timeline; each task's work is a Step Functions execution with its history in the AWS Console. Familiar to anyone on-call for AWS. | Scheduler UI + per-task logs, one dashboard per orchestrator. |
| **Where state lives** | DynamoDB — pay per request, no schema to migrate, no cluster to administer. | Postgres you host, patch, and upgrade. |
| **Assets** | `outlets=`, `wait_for=` and asset-triggered schedules, experimental; `polyris-output --assets` lists the assets a pipeline file declares, with their producers and consumers in that file. | Native in Dagster; Airflow 3 has assets too. |

Full DSL reference — `retries`, `trigger_rule`, seven task types (`sfn`, `lambda`, `glue`, `ecs`, `athena`, `emr`, `batch`), asset schedules, `wait_for` freshness checks, `xcom` data passing — in [DSL.md](docs/features/DSL.md).

### polyris or Amazon MWAA Serverless

MWAA Serverless runs Airflow 3 with no environment to keep running, so it serves the same teams. Where the two differ:

| | polyris | Amazon MWAA Serverless |
|---|---|---|
| **Pipelines** | Python | YAML in the DAG Factory format; Python DAGs can be converted |
| **A task's work** | Step Functions calls Glue, Lambda, ECS, Batch, Athena, EMR or a state machine directly | Each task gets its own compute, provisioned before the task starts |
| **A failed task** | Waits in the run for your decision: retry, mark success, skip or fail | Airflow's retries; the hosted Airflow UI has a subset of its features |
| **Integrations** | Seven task types | Airflow operators: AWS, Python, Bash, provider packages and your own |
| **Where it runs** | A platform you install in your own account; open source | A service AWS runs; tasks can run in your VPC |
| **Billing** | Per state transition, function call and request: see [Cost](#cost) | Per task for its duration, $0.08 an hour, at least one minute |

Choose MWAA Serverless for Airflow's operators with nothing to install or upgrade. Choose polyris for Python pipelines over the AWS services you already run, a console that acts on a live run, and nothing outside your account.

## Install

Requires **Python 3.11+** and AWS credentials — no git, Node.js, SAM CLI or AWS CLI. Install the CLI, then the platform into your AWS account:

```bash
curl -fsSL https://github.com/Polyris/polyris/releases/latest/download/install.sh | bash
polyris-up                          # asks for a namespace, a stage and an admin email; prints the console URL
polyris-init hello-world --hello    # a first pipeline that deploys and runs without edits
cd hello-world && polyris-deploy    # then press Run on hello-world in the console
```

`polyris-up` deploys the prebuilt release of your polyris version. Step by step: [QUICKSTART.md](docs/getting-started/QUICKSTART.md); upgrading, stages, users and removal: [manage-the-platform.md](docs/how-to/manage-the-platform.md).

## Try it locally (no AWS)

With the CLI from [Install](#install), you can validate a pipeline, generate its Step Functions JSON and mock-run it without an AWS account:

```bash
polyris-init my-pipeline --local
cd my-pipeline

polyris-validate              # Validate structure
polyris-validate -v           # Verbose: schedule and task count
polyris-output --json         # The definition and its task specs
polyris-output --mermaid      # Mermaid diagram source
polyris-output --graph        # ASCII graph
```

CLI (run from the pipeline directory):

```bash
polyris-local             # dry run — execution plan + validation
polyris-local --validate  # exit 0/1, suitable for CI
polyris-local --mock      # simulate execution with mock results
```

Python API for automated tests. Install polyris into your test environment, at the version of your CLI (`v1.3.0` here):

```bash
pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
```

Then, from the pipeline's folder:

```python
from polyris.local import validate, dry_run, run
from dag import dag                        # dag.py in this folder

validate(dag)                              # DAG structure
dry_run(dag)                               # Execution plan
result = run(dag, mock=True)               # Mock execution
print(result.summary())                    # ✅ 3 succeeded, ❌ 0 failed, ⏭️ 0 skipped
```

Or browse [examples/](examples/) — 17 self-contained pipelines from hello-world through assets.

## Documentation

| I want to... | Go to |
|---|---|
| Set up polyris from a blank AWS account | [QUICKSTART.md](docs/getting-started/QUICKSTART.md) |
| Learn the Python DSL — every task type, parameter, trigger rule | [DSL.md](docs/features/DSL.md) |
| Test pipelines locally (validate / dry_run / mock) | [LOCAL_TESTING.md](docs/tools/LOCAL_TESTING.md) |
| Pass data between tasks (xcom) | [DATA_PASSING.md](docs/features/DATA_PASSING.md) |
| Package xcom helpers into a Lambda zip (no pip step) | [DATA_PASSING.md](docs/features/DATA_PASSING.md) |
| Configure retries, backoff, jitter | [how-to/configure-retries.md](docs/how-to/configure-retries.md) |
| Schedule a pipeline, pause, redeploy safely | [how-to/schedule-and-redeploy.md](docs/how-to/schedule-and-redeploy.md) |
| Set up asset-based orchestration + `wait_for` | [ASSETS.md](docs/features/ASSETS.md) |
| Use the Web Console — DAG view, Runs, task actions | [UI.md](docs/operations/UI.md) |
| Set up Cognito authentication | [authentication.md](docs/features/authentication.md) |
| Lay out a repository of pipelines, with CI | [PROJECT_STRUCTURE.md](docs/getting-started/PROJECT_STRUCTURE.md) |
| Look up a term (run, task, asset, stage…) | [GLOSSARY.md](GLOSSARY.md) |
| Talk to polyris over REST | [API.md](docs/operations/API.md) |
| Approve an install (IAM / resource inventory) | [IAM_PERMISSIONS.md](docs/deployment/IAM_PERMISSIONS.md) + [INFRASTRUCTURE.md](docs/deployment/INFRASTRUCTURE.md) |
| Fix a specific problem | [TROUBLESHOOTING.md](docs/operations/TROUBLESHOOTING.md) |
| Understand the runtime architecture | [ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md) |
| Read design decisions / ADRs | [adr-index.md](docs/decisions/adr-index.md) |
| Develop polyris itself | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Cost

polyris runs on Step Functions + Lambda + DynamoDB — all pay-per-request, no fleet to keep alive:

| Component | AWS billing model | Approx. per-run share |
|---|---|---|
| Step Functions Standard | Per state transition ($0.025 / 1k) | $0.001 – $0.005 |
| Lambda (task orchestration) | Per invocation + duration | ~$0.0001 per task |
| DynamoDB | Per read/write unit | ~$0.001 per run |
| CloudWatch Logs | GB ingested | $0.50 – $2 / month total |

**Small deployment (10 pipelines, daily, ~10 tasks each) — ~$2–4/month for orchestration.** Your task workloads (Glue jobs, Lambda functions, ECS containers, Athena queries) bill separately against your existing AWS spend; polyris does not add a percentage on top.

Every resource in a polyris stack carries `polyris:` tags, so Cost Explorer can show what polyris costs per installation and stage: see [Track costs](docs/how-to/manage-the-platform.md#track-costs).

**Amazon MWAA** with an environment costs $0.29 an hour for its smallest class, `mw1.micro`: about $210 a month whether or not a pipeline runs, 50 to 100 times the small deployment above. **MWAA Serverless** bills each task for its duration, $0.08 an hour with a one-minute minimum: about $4 a month for the small deployment if every task finishes within a minute, and more for tasks that run longer. What polyris costs grows with the steps of a run, not with how long they take: a task that waits an hour for its Glue job costs about what a one-minute task does.

*Numbers are estimates at AWS list prices in us-east-1 as of November 2026 — check AWS pricing pages for current figures.*

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the repository's layout, testing, and PR guidelines. Inside a polyris checkout:

```bash
pip install -e ".[dev]"   # adds pytest, ruff, mypy
make test                 # Run all tests
make check                # Lint + sync + test (before PR)
```

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
