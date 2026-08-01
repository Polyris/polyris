# CLI Reference

All polyris commands. Run from your pipeline directory unless noted.

---

## polyris

Top-level dispatch command (v0.78+). Without arguments prints help.
Subcommands route to backfill operations against a deployed Console API.

```bash
polyris                                    # show help
polyris backfill pipeline NAME [opts]      # backfill a pipeline
polyris backfill asset NAME [opts]         # backfill an asset
polyris backfills list [--status ...]      # list recent backfills
polyris backfills show ID                  # show backfill detail
polyris backfills cancel ID                # cooperatively cancel
polyris backfills retry-failed ID          # fork new backfill with failed partitions
```

### Configuration

The `backfill*` subcommands talk to the Console API over HTTPS. Configure
via env vars:

```bash
export POLYRIS_API_URL=https://abc123.execute-api.us-east-1.amazonaws.com/Prod
export POLYRIS_API_TOKEN=<optional-bearer-token>   # if API is protected
```

### `polyris backfill pipeline`

Start a backfill targeting a pipeline. The pipeline's cron schedule is
read at runtime and used to infer partition granularity (per ADR #52);
ambiguous schedules default to `daily` with a warning.

```bash
polyris backfill pipeline daily-etl \
    --start 2024-01-15 --end 2024-01-20 \
    --max-parallel 5 \
    --tasks extract,transform \
    --variables '{"region": "us-east"}' \
    --preview            # show plan without starting
```

Options:
- `--start DATE` (required) — start partition key or date
- `--end DATE` (required) — end partition key or date
- `--tasks LIST` — comma-separated task names (subset of pipeline)
- `--max-parallel N` — Map concurrency 1–10 (default 5)
- `--force` — bypass safety checks
- `--no-skip-completed` — re-run already-completed partitions
- `--incremental` — stop on first failure
- `--variables SPEC` — JSON object or `k1=v1,k2=v2`
- `--preview` — return the plan + cost estimate without starting

### `polyris backfill asset`

Start a backfill targeting an asset. The producer pipeline is resolved
from `pipeline_registry` outlets; multiple producers fail with a list of
candidates so you can re-issue with `target.type=pipeline`.

```bash
polyris backfill asset catalog/db/orders \
    --start 2024-01-15 --end 2024-01-15 \
    --cascade auto
```

Same options as `backfill pipeline`, plus:
- `--cascade auto|all|none` — cascade strategy for downstream consumers
  (default `auto` = respect trigger rules)

### `polyris backfills list`

```bash
polyris backfills list                          # all recent
polyris backfills list --status active          # pending + running
polyris backfills list --status failed --limit 25
```

Status values: `active`, `pending`, `running`, `completed`, `failed`,
`partial`, `canceled`.

### `polyris backfills show ID`

```bash
polyris backfills show bf-a1b2c3d4
```

Returns full backfill record with partition keys, child executions, and
parsed options.

### `polyris backfills cancel ID`

```bash
polyris backfills cancel bf-a1b2c3d4
```

Marks status as `canceled` in DDB. The bulk-backfill SFN's Map iterator
checks at each iteration and short-circuits remaining partitions. In-
flight child executions continue to completion (per ADR #54).

Returns exit code 1 with `already_terminal` if the backfill is already
done.

### `polyris backfills retry-failed ID`

```bash
polyris backfills retry-failed bf-a1b2c3d4
```

Creates a new Backfill containing only the failed partitions of the
parent, linked via `parent_backfill_id`. Only valid when parent status
is `failed` or `partial`.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | API returned 4xx/5xx (response printed to stderr) |
| 2 | Missing or invalid configuration (`POLYRIS_API_URL` unset) |
| 3 | Network/transport failure |
| 4 | Bad CLI arguments |
| 130 | Interrupted (Ctrl-C) |

---

## polyris (legacy help dispatcher)

For backward compat, `polyris` without subcommand prints help text. Use
the named entry points below for deployment workflows.

---

## polyris-init

Create a new pipeline or initialize project config.

```bash
# Initialize project config.py (run once in project root)
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
| `--project` | Generate `config.py` template in current directory |
| `--local` | Create pipeline without AWS (for exploring DSL) |
| `--schedule` | Cron schedule (default: `@daily`) |
| `--dir` | Base directory (default: `.`) |
| `-i`, `--interactive` | Interactive wizard |

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
polyris-deploy --destroy

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
polyris-deploy --destroy --all
polyris-deploy --destroy --only dir1 dir2

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
   not just `dag.py`. A directory with several independent pipeline files
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
