# CLI Reference

All polyris commands. Run from your pipeline directory unless noted.

---

## polyris

Top-level help dispatcher. Prints the list of available commands and exits.
Each command is a separate console script — run any with `--help` for options.

```bash
polyris    # print the command index
```

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
| `--stage` | Stage of a new install (default: `dev`). Namespace + Stage ≤ 30 characters |
| `--admin-email` | First console admin; Cognito emails a temporary password |
| `--param KEY=VALUE` | Set a template parameter (repeatable). Unset parameters keep their current value. `Namespace`, `Stage`, `ArtifactsBucket`, `UiArtifactsPrefix` are refused |
| `--stack` | Platform stack name (default: the one stack tagged as a Polyris platform, or `<namespace>-<stage>` when those flags are given). Naming a platform stack created with `sam deploy` switches it to `polyris-up` (asks to confirm); a stack that isn't a Polyris platform is refused |
| `--profile`, `--region` | AWS profile / region (default: `AWS_REGION`, then the standard credential chain; no region anywhere is an error) |
| `--archive` | Deploy a local release archive (from `scripts/build_platform_release.py`) instead of downloading one |
| `--yes` | Don't ask for confirmation |
| `--accept-data-loss` | Allow a change that replaces or removes a DynamoDB table, S3 bucket, the Cognito user pool or the admin user. `--yes` never does |
| `--allow-downgrade` | Allow installing an older release than the stack runs (a rollback). Without it `polyris-up` refuses |

The release archive is cached in `~/.cache/polyris/<version>/`; artifacts go to the bucket `polyris-artifacts-<account>-<region>`. A change that would replace or remove a DynamoDB table, S3 bucket, the Cognito user pool or the admin user is listed and needs confirmation (or `--accept-data-loss`).

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

It refuses a stack that isn't a Polyris platform and a stack that is mid-update. Unless you pass `--yes`, it also refuses while pipelines are still deployed on the platform: every pipeline in its registry whose state machine still exists, plus stacks named `<namespace>-<stage>-polyris-*`. Remove those first with `polyris-destroy`. The artifacts bucket is deleted when no other platform stack is left in the region.

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

# Validate all pipelines found in project
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
| `--all`, `-a` | Find and validate all pipelines in project |
| `-v`, `--verbose` | Show schedule, task count, state count |
| `--file`, `-f` | Pipeline file (default: `dag.py`) |
| `--test` | Run python_callable for `@task.python` tasks |
| `--json` | Output results as JSON |

---

## polyris-output

Generate pipeline artifacts. Run from pipeline directory.

```bash
# Generate Step Functions ASL JSON
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
| `--json` | Generate Step Functions ASL JSON |
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

# Bulk: deploy every subdirectory of the current directory
# (each directory's every .py file is scanned; every DAG found is deployed;
# a file with zero DAGs — a shared config.py, etc. — is silently skipped)
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
| `--destroy` | Remove pipeline stack and clean up DynamoDB |
| `--select` | Select DAG by ID (for multi-DAG files); combinable with `--all`/`--only` to filter within each directory |
| `--all` | Bulk: every immediate subdirectory of the current directory. Mutually exclusive with `--only` |
| `--only DIR [DIR ...]` | Bulk: only the listed subdirectories. Mutually exclusive with `--all` |
| `--region` | AWS region (default: from `config.py`) |
| `--log-level` | CloudWatch log level: `ALL`, `ERROR`, `FATAL`, `OFF` (default: `ERROR`) |
| `--log-retention` | Log retention in days (default: `30`) |

### Bulk mode (`--all` / `--only`)

Run from the parent directory containing your pipeline subdirectories (e.g.
`examples/`, with `cd examples`). Each target directory is scanned
independently:

1. Every `.py` file directly in the directory is loaded (non-recursive) —
   not only `dag.py`. A directory with several independent pipeline files
   (`orders.py`, `inventory.py`, ...) has all of them deployed.
2. Every DAG object found in every file is deployed — a file with several
   DAGs (see the multi-DAG-file note above) gets all of them, same as
   single-file mode.
3. A file with zero DAGs (a shared `config.py`, a `utils.py`) contributes
   nothing and is silently skipped — not an error.

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
