# CLI Reference

All polyris commands. Run from your pipeline directory unless noted.

`polyris-validate`, `polyris-deploy`, `polyris-output` and `polyris-local` read a pipeline file the same way, so a file that validates also deploys. A pipeline is every DAG the file's code makes — with or without `with`, through `@dag`, a helper function, or other files it runs — and not one that a module it imports makes. While the file runs, its folder and then the folder above it come first on `sys.path`, and `sys.exit(0)` ends it normally. Each DAG is one stack, so a `dag_id` made twice is refused. With `--all`, both commands load the folders directly in the current directory — each in its own Python process, so one folder's modules never reach another's — and in each, `dag.py` and every `.py` whose code imports `DAG`, `dag` or `Pipeline` from polyris, except test files: a Glue script that imports only `xcom` is never run, whatever its comments say. Both warn about a pipeline file in the current directory itself or deeper, which `--all` does not load, and refuse a `dag_id` that two files make.

---

## polyris

Top-level help dispatcher. Prints the list of available commands and exits.
Each command is a separate console script — run any with `--help` for options.

```bash
polyris              # print the command index
polyris --version    # print the installed version, e.g. "polyris 1.2.2"
```

The installed version is also the platform version `polyris-up` installs, so quote it in a bug report.

---

## polyris-up

Install or upgrade the Polyris platform in your AWS account from the prebuilt release of the installed polyris version. Runs from any directory and writes nothing there.

```bash
polyris-up                                   # new install (asks for namespace, stage, admin email) or upgrade
polyris-up --namespace acme --stage dev --admin-email you@acme.com --yes
polyris-up --param EnablePitr=true           # change one template parameter, keep the rest
polyris-up --namespace acme --stage prod     # a second stage next to an existing one
```

| Option | Description |
|--------|-------------|
| `--namespace` | Resource prefix for a new install. Bucket names are global, so it must be unique to you |
| `--stage` | Stage of a new install (default: `dev`). Namespace + Stage ≤ 33 characters |
| `--admin-email` | First console admin; Cognito emails a temporary password |
| `--param KEY=VALUE` | Set a template parameter (repeatable). Unset parameters keep their current value. `Namespace`, `Stage`, `ArtifactsBucket`, `UiArtifactsPrefix` are refused |
| `--stack` | Platform stack name (default: the one stack tagged as a Polyris platform, or `<namespace>-<stage>` when those flags are given). Naming a platform stack created with `sam deploy` switches it to `polyris-up` (asks to confirm); a stack that isn't a Polyris platform is refused |
| `--profile`, `--region` | AWS profile / region (default: `AWS_REGION`, then the standard credential chain; no region anywhere is an error) |
| `--archive` | Deploy a local release archive (from `scripts/build_platform_release.py`) instead of downloading one |
| `--yes` | Don't ask for confirmation |
| `--accept-data-loss` | Allow a change that replaces or removes a DynamoDB table, S3 bucket, the Cognito user pool or the admin user. `--yes` never does |
| `--allow-downgrade` | Allow installing an older release than the stack runs (a rollback). Without it `polyris-up` refuses. A pre-release such as `1.3.0rc1` counts as older than `1.3.0`. No flag rolls a platform that runs 1.3 back below 1.3: a pipeline deployed for 1.3 would not run on 1.2 |
| `--disable-rollback` | If the update fails, keep what it already changed (`UPDATE_FAILED`) instead of rolling back. Fix the cause and run the same command again; if the release has nothing more to change, retry the failed update from the CloudFormation console. A first install always rolls back. Use it when the release notes ask for it |
| `--accept-stopped-runs` | Upgrade a 1.2 platform to 1.3 although a run may be in flight (one is, or the check found none of the engine's state machines), or although 1.2 finished adopting older rows and facts less than 48 hours ago. `polyris-up` stops what 1.2 left running and prints the runs to start again. It never covers a platform older than 1.2.0 |

The release archive is cached in `~/.cache/polyris/<version>/`; artifacts go to the bucket `polyris-artifacts-<account>-<region>`. A change that would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user is listed and needs confirmation (or `--accept-data-loss`).

Before it upgrades a platform below 1.3, `polyris-up` checks it, and refuses one that is older than 1.2.0, whose 1.2 has not finished adopting older rows and facts for 48 hours, or that has a run in flight. Each refusal says what to do. The upgrade stops the executions that 1.2 left running and prints the runs it stopped; a later `polyris-up` does so again only while a state machine of 1.2 is left, or the stack still cleans up after an interrupted upgrade. On a platform that runs 1.3, every `polyris-up` waits until DynamoDB has built `run-start-index`, which the console's run lists read, and lists the pipelines that still need a redeploy, with the command for each. The steps of that upgrade: [manage-the-platform.md](../how-to/manage-the-platform.md#upgrade-from-12-to-13).

---

## polyris-down

Delete the platform stack. Lists the DynamoDB tables (always deleted) and buckets with their object counts, then asks you to type the stack name.

```bash
polyris-down                    # refuses while ResultsBucket holds task results
polyris-down --delete-data      # also delete task results
```

| Option | Description |
|--------|-------------|
| `--delete-data` | Empty and delete `ResultsBucket` too |
| `--stack`, `--profile`, `--region` | As for `polyris-up` |
| `--yes` | Don't ask for confirmation |

It refuses a stack that isn't a Polyris platform and a stack that is mid-update. Unless you pass `--yes`, it also refuses while pipelines are still deployed on the platform: every pipeline in its registry whose state machine still exists, plus stacks named `<namespace>-<stage>-polyris-*`. Remove those first with `polyris-destroy`. Before it empties the buckets, and until the stack is gone, it stops the running executions of the platform's state machines and prints how many of each. The artifacts bucket is deleted when no other platform stack is left in the region.

---

## polyris-init

Create a new pipeline or initialize project config.

```bash
# A first pipeline that runs right away on the installed platform
polyris-init hello-world --hello

# Initialize project — writes pipelines/config.py (creates pipelines/ if missing).
polyris-init --project

# Create a new pipeline (creates my-pipeline/dag.py)
polyris-init my-pipeline

# Try locally without AWS (explore DSL)
polyris-init my-pipeline --local

# Custom schedule
polyris-init my-pipeline --schedule "@hourly"
polyris-init my-pipeline --schedule "0 9 * * MON-FRI"

# Interactive wizard
polyris-init my-pipeline -i

# Custom directory
polyris-init my-pipeline --dir ./pipelines
```

| Option | Description |
|--------|-------------|
| `name` | Pipeline name (optional with `--project` or `-i`) |
| `--project` | Generate `pipelines/config.py` (creates `pipelines/` if missing) |
| `--hello` | Scaffold a pipeline that calls the platform's built-in test Step Function — deploys and runs without edits. Finds the platform like `polyris-deploy` does; accepts `--profile`, `--region`, `--stack` |
| `--local` | Create pipeline without AWS (for exploring DSL) |
| `--schedule` | Cron schedule (default: `@daily`) |
| `--dir` | Base directory (default: `.`) |
| `-i`, `--interactive` | Interactive wizard |

---

## polyris-local

Preview, validate, and simulate a pipeline without deploying to AWS. Run from the pipeline directory.

```bash
# Dry run — shows DAG config, execution order, and validation result
polyris-local

# Validate and exit 0 (valid) or 1 (invalid)
polyris-local --validate

# Simulate execution with mock results
polyris-local --mock

# Dry run + generated Step Functions ASL JSON
polyris-local --dry-run --asl

# Use a different pipeline file (default: dag.py)
polyris-local --file other.py
```

| Option | Description |
|--------|-------------|
| `--validate` | Validate structure and exit 0/1 |
| `--mock` | Simulate execution with mock results |
| `--localstack` | Execute against LocalStack at localhost:4566 |
| `--dry-run` | Show execution plan (default when no mode given) |
| `--asl` | Include generated Step Functions JSON in dry-run output |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |

---

## polyris-validate

Validate pipeline(s) for errors. Run from pipeline directory.

```bash
# Validate dag.py in current directory
polyris-validate

# Verbose — shows schedule, task count, state count
polyris-validate -v

# Validate every pipeline folder in the current directory, as polyris-deploy --all deploys them
polyris-validate --all

# All pipelines, verbose
polyris-validate --all -v

# Specific file
polyris-validate --file my_pipeline.py

# Run python_callable for @task.python tasks
polyris-validate --test

# JSON output
polyris-validate --json
```

| Option | Description |
|--------|-------------|
| `--all`, `-a` | Validate the pipeline files of each folder directly in the current directory, as `polyris-deploy --all` deploys them, and check them against each other (asset cycles, a `dag_id` made twice) |
| `-v`, `--verbose` | Show schedule, task count, state count |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |
| `--test` | Run python_callable for `@task.python` tasks |
| `--json` | Output results as JSON |

---

## polyris-output

Generate pipeline artifacts. Run from pipeline directory.

```bash
# The Step Functions definition and the task specs it names
polyris-output --json

# Generate Mermaid diagram
polyris-output --mermaid

# Show DAG as ASCII graph
polyris-output --graph

# Generate asset registry JSON
polyris-output --assets

# Custom file
polyris-output --json --file my_pipeline.py

# Multi-DAG file — select specific DAG
polyris-output --json --select my-dag-id
```

| Option | Description |
|--------|-------------|
| `--json` | The Step Functions definition (`definition`) and the task specs polyris-deploy writes to `pipeline-specs` (`specs`: `spec_id`, `task_count`, `format_version` and each task's spec) |
| `--mermaid` | Generate Mermaid diagram |
| `--graph` | Show DAG as ASCII graph |
| `--assets` | Generate asset registry JSON |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |
| `--select` | Select DAG by ID (for multi-DAG files) |

---

## polyris-deploy

Deploy pipeline to AWS via CloudFormation. Run from pipeline directory.

```bash
# Deploy using defaults from config.py
polyris-deploy

# Deploy to specific stage
polyris-deploy --stage prod

# Override AWS profile
polyris-deploy --profile my-aws-profile

# Both
polyris-deploy --stage prod --profile my-aws-profile

# Preview without deploying
polyris-deploy --dry-run

# Remove pipeline stack
polyris-destroy

# Deploy specific file
polyris-deploy --file my_dag.py

# Multi-DAG file — deploy specific DAG
polyris-deploy --select my-dag-id

# Bulk: deploy every folder in the current directory (hidden ones and caches aside)
# (each directory in its own process; dag.py and every .py whose code imports
# DAG, dag or Pipeline from polyris are loaded — other files are listed and not
# run; every DAG found is deployed; a file with zero DAGs is skipped; a file that
# does not load counts as a failure: the summary lists it, the other directories
# still deploy, exit code 1; a dag_id two files make deploys nothing, exit code 1)
polyris-deploy --all

# Bulk: deploy only the listed subdirectories
polyris-deploy --only dir1 dir2

# Bulk + destroy
polyris-destroy --all
polyris-destroy --only dir1 dir2

# Bulk + pick one DAG per directory (directories without that DAG are skipped,
# not an error)
polyris-deploy --only dir1 dir2 --select my-dag-id
```

| Option | Description |
|--------|-------------|
| `--stage` | Deployment stage (default: from `config.py DEFAULT_STAGE`) |
| `--profile` | AWS profile (default: from `config.py` or AWS default) |
| `--file` | Pipeline file (default: `dag.py`). Not used with `--all`/`--only` |
| `--dry-run` | Preview CloudFormation changes without deploying |
| `--destroy` | Remove the pipeline's stack, stopping its running runs until the stack is gone |
| `--select` | Select DAG by ID (for multi-DAG files); combinable with `--all`/`--only` to filter within each directory |
| `--all` | Bulk: every folder directly in the current directory, but hidden ones (`.venv`) and caches. Mutually exclusive with `--only` |
| `--only DIR [DIR ...]` | Bulk: only the listed subdirectories. Mutually exclusive with `--all` |
| `--region` | AWS region (default: from `config.py`) |
| `--log-level` | CloudWatch log level: `ALL`, `ERROR`, `FATAL`, `OFF` (default: `ERROR`) |
| `--log-retention` | Log retention in days (default: `30`) |

### Bulk mode (`--all` / `--only`)

Run from the parent directory containing your pipeline subdirectories (e.g.
`examples/`, with `cd examples`). Each target directory is scanned
independently:

1. `dag.py` and every `.py` file directly in the directory whose code
   imports `DAG`, `dag` or `Pipeline` from polyris are loaded
   (non-recursive), except test files, in a Python process of the
   directory's own. A directory with
   several independent pipeline files (`orders.py`, `inventory.py`, ...) has
   all of them deployed; a Glue script beside them is never run.
2. Every DAG object found in every file is deployed — a file with several
   DAGs (see the multi-DAG-file note above) gets all of them, same as
   single-file mode.
3. A pipeline file with zero DAGs contributes nothing and is skipped — not
   an error. A shared `config.py` or `utils.py` that imports no DAG maker is
   listed as not a pipeline file and never run.
4. Before anything is deployed, every directory's `dag_id`s are collected:
   a `dag_id` that two files make (in one directory or two) stops the run,
   and nothing is deployed — each DAG is one stack, so one would replace the
   other. With `--select`, only that `dag_id` counts. A directory whose
   `dag_id`s could not be read (its process ended without answering) is not
   deployed and counts as a failure.
5. With `--all`, a pipeline file in the current directory itself, or deeper
   than one folder, is not deployed: a warning names it. Move it into its own
   folder, or deploy it with `--file`.

One DAG failing (a validation error, an AWS error) does not stop the batch —
every DAG in every directory is still attempted, and a summary prints at the
end listing what succeeded and what failed. The process exits non-zero if
anything failed, so it's safe to use as a CI/script gate.

`--file` is not used in bulk mode (each directory's files are discovered
automatically) — passing both is a usage error.

---

## polyris-register

Manually register a pipeline in DynamoDB (without running tasks).

```bash
# Register by ARN
polyris-register arn:aws:states:us-east-1:123456789:stateMachine:my-pipeline

# Register by name
polyris-register --name my-pipeline

# With specific profile and region
polyris-register --name my-pipeline --profile prod --region us-east-1

# Assume IAM role (cross-account)
polyris-register --name my-pipeline --role-arn arn:aws:iam::123:role/deploy

# JSON output
polyris-register --name my-pipeline --json
```

| Option | Description |
|--------|-------------|
| `arn` | Step Function ARN (positional, optional) |
| `--name`, `-n` | Pipeline name (alternative to ARN) |
| `--region`, `-r` | AWS region (default: from `config.py` or `us-east-1`) |
| `--profile`, `-p` | AWS profile from `~/.aws/credentials` |
| `--namespace` | Namespace prefix for pipeline search |
| `--role-arn` | IAM role ARN to assume (cross-account) |
| `--json` | Output result as JSON |
