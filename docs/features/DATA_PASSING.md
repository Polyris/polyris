# Passing data between tasks

Task A returns some data. Task B reads it. This page shows how, per task type, in as little code as possible.

## Requires

`polyris >= 0.100.0` in your task deployment bundles (Lambda zip, Glue `--additional-python-modules`, ECS container install, etc.). The new `xcom.get()` / `xcom.push()` API and typed error classes ship in that version.

Older SDK versions keep working with the legacy `event["upstream"][X]["output"]` / `xcom.pull()` patterns described further down — you just don't get the new features.

## Cheat sheet — one call, every task type

```python
from polyris import xcom

# Lambda handler — reads pre-fetched upstream from event
def handler(event, context):
    sales = xcom.get(event, "extract_sales")     # → whatever extract_sales returned
    return {"rows": len(sales["items"])}

# Glue / ECS / Batch job code — reads DDB directly
def main():
    from polyris import xcom
    sales = xcom.get(None, "extract_sales")      # → same shape, same errors
    ...
```

Writes:

```python
# Lambda — just return
def handler(event, context):
    return {"rows": 1240, "path": "s3://lake/2026-01-01/data.parquet"}

# Glue / ECS / Batch — call xcom.push() before the job ends
def main():
    df.write.parquet("s3://lake/2026-01-01/data.parquet")
    xcom.push({"rows": df.count(), "path": "s3://lake/2026-01-01/data.parquet"})
```

That's the whole mental model. The rest of this page explains the details.

## Reading upstream: `xcom.get()`

```python
xcom.get(event, task_name, *,
         raise_on_missing=True,     # dep with no output → XComMissingError
         raise_on_failure=True,     # upstream status != success → XComUpstreamFailedError
         raise_on_manual=True,      # upstream manually resolved via UI → XComManuallyResolvedError
         ddb_client=None,           # injected for tests
         s3_client=None)            # injected for tests
```

Lookup order:

1. If `event.upstream[task_name]` exists (Lambda / SFN pre-fetched inject) → use it. Auto-resolves `{"_s3_ref": "s3://..."}` pointers. On a `{"_truncated": true}` marker, falls back to DDB automatically (25KB inject cap vs 350KB DDB row).
2. Otherwise → reads DDB directly via `pull()`. Works with `event=None` for Glue / ECS / Batch / EMR.

Optional upstream with `trigger_rule="all_done"`:

```python
def handler(event, context):
    sales = xcom.get(event, "extract_sales")                                # required — raises if missing
    bonus = xcom.get(event, "bonus_source", raise_on_failure=False) or {}   # optional — None if failed
```

Failure taxonomy:

- `XComMissingError` — no output stored (dep didn't run, was skipped, or returned nothing).
- `XComUpstreamFailedError` — upstream status is `skipped`, `failed`, or `aborted`.
- `XComTruncatedError` — both the inject and DDB row came back truncated. Use the Claim Check pattern (below).
- `XComManuallyResolvedError` — upstream carries a Console-written manual-resolution marker (an operator clicked Mark success / Skip / Fail / Stop on it via UI). The recorded "output" is a synthetic marker, not organic payload — reading it as data crashes with `KeyError` at the first attribute access. The error exposes `resolution` / `operator` / `reason` for routing or logging.

All four inherit `XComError`, which inherits `RuntimeError`. Existing `except PullError:` code still catches `XComMissingError` — `PullError` is now an alias.

Reading a manually-resolved upstream (rare — routes on the operator's intent):

```python
try:
    sales = xcom.get(event, "extract_sales")
except XComManuallyResolvedError as e:
    log.warn(f"upstream {e.task_name} was {e.resolution} by {e.operator}: {e.reason}")
    # Introspect the marker verbatim if the resolution matters to control flow:
    marker = xcom.get(event, "extract_sales", raise_on_manual=False)
```

## Writing output

### Lambda / `@task.sfn` — return your value

The wrapper captures the Lambda return value or the child SFN's `Output`. No SDK call needed. This is the idiomatic path.

```python
def handler(event, context):
    return {"rows": 100, "path": "s3://..."}
```

**Do not** call `xcom.push()` from a Lambda handler — see [Lambda write pattern](#lambda-write-pattern) below.

### Glue / ECS / Batch / EMR — call `xcom.push()`

Service tasks run in their own containers/jobs — the wrapper only sees the AWS API response (`{JobRunId}`, `{TaskArn}`, `{JobId}`, `{QueryExecutionId}`, `{StepId}`), not the work output. `xcom.push()` writes the real value directly to DDB.

```python
# Inside your Glue Spark job:
from polyris import xcom

def main():
    df = spark.read.parquet("s3://raw/...")
    df.write.parquet("s3://lake/curated/2026-01-01/data.parquet")
    xcom.push({
        "rows": df.count(),
        "path": "s3://lake/curated/2026-01-01/data.parquet",
    })
```

The push writes to the same DDB key the wrapper writes on success (`output#{pipeline}#{task}#{date}`). The wrapper detects a `_pushed_by_task` marker with a matching `pushed_run_id` and skips overwriting the pushed value with the AWS API response.

**EMR is not supported in the current release** — `addStep.sync` has no Environment field to inject `POLYRIS_TASK_NAME`, and injecting it via `HadoopJarStep.Args` would risk breaking user Spark arg parsers. Deferred to a follow-up.

### Athena — no SDK, use SQL variables or a Lambda front

Athena SQL can't call `xcom.push()`. Two patterns:

- Small values → set them on `variables=` at the DAG level and reference from the query via templating.
- Otherwise → put a Lambda after Athena. The Lambda reads Athena's result location from `event.upstream["athena_task"]["output"]["QueryExecutionId"]`, resolves the query result, and returns / pushes the actual value.

## The service task metadata trap

Without `xcom.push()`, a service task's stored `result` is the AWS API response, not application data. Downstream sees:

| Task type | Stored `result` without push |
|-----------|------------------------------|
| `lambda_function` | Lambda return value (real data — no push needed) |
| `sfn` | Child SFN Output (real data — no push needed) |
| `glue_job` | `{"JobRunId": ...}` |
| `ecs_task` | `{"Tasks": [{"TaskArn": ...}]}` |
| `athena_query` | `{"QueryExecutionId": ...}` |
| `batch_job` | `{"JobId": ...}` |
| `emr_step` | AWS response with step identifier |

Console flags this: when the Output tab detects an AWS-response shape, a banner appears with the hint to call `xcom.push()`.

## Lambda write pattern

`xcom.push()` from a Lambda handler races with the wrapper's `Save_Success` write. If the push completes before the handler returns, it's fine; if it happens asynchronously after return, ordering is undefined and downstream may see the wrong value.

- Prefer `return value` from Lambda — the wrapper captures it deterministically.
- `xcom.push()` from a Lambda emits a `UserWarning` explaining this.
- If you call both `xcom.push({A})` and `return {B}`, the pushed value wins (the wrapper detects the marker and doesn't overwrite `result`). Pick one; don't mix.

## Size limits

| Constraint | Limit | Fix at the limit |
|-----------|-------|------------------|
| Runtime inject per dep | 25 KB | Use `xcom.get()` — auto-falls-back to DDB (~350 KB). |
| DDB `result` field | 350 KB | Use Claim Check pattern (below). |
| Console preview (`task_input`) | ~380 KB | New in 0.100.0 — separate `input#` DDB record. |
| DDB item hard limit (AWS) | 400 KB | AWS constraint. |

For > 350 KB payloads, use the Claim Check pattern.

## Large outputs — Claim Check pattern (Bring Your Own bucket)

Real data pipelines write big values to S3 as parquet / JSON with a proper schema, and XCom carries a pointer, not the data itself. If a task genuinely needs to hand off a large blob via XCom, use the manual claim-check convention:

```python
import boto3, json
from polyris import xcom

s3 = boto3.client("s3")

def handler(event, context):
    big_data = compute_something_large()
    key = f"xcom-payloads/{event['pipeline_name']}/{event['date']}/data.json"
    s3.put_object(
        Bucket="my-data-lake",
        Key=key,
        Body=json.dumps(big_data).encode(),
    )
    return {"_s3_ref": f"s3://my-data-lake/{key}"}

# Downstream — nothing special:
def downstream(event, context):
    data = xcom.get(event, "upstream_task")     # xcom.get() auto-resolves _s3_ref
```

Bring your own bucket. Bring your own IAM (`s3:PutObject` for the writer, `s3:GetObject` for the reader). The polyris-managed `PolyrisTaskReadPolicy` grants only DDB read, not S3 read on user buckets.

## Installing polyris SDK in Glue / ECS / Batch

`xcom.get()` / `xcom.push()` live in the `polyris` package. Your task code needs to import it.

**Glue** — `--additional-python-modules`:

```yaml
DefaultArguments:
  "--additional-python-modules": "polyris==0.100.0"
```

Or bake it into a wheel and pass via `--extra-py-files`.

**ECS / Batch** — install into your container image:

```dockerfile
RUN pip install polyris==0.100.0
```

**Lambda** — ship in the deployment zip (typical `requirements.txt` for your Lambda function).

**EMR** — bootstrap script that `pip install polyris==0.100.0` on cluster nodes. (Reads work — `xcom.get()` from EMR job code just needs the DDB permissions from `PolyrisTaskReadPolicy`. Writes via `xcom.push()` are not supported in 0.100.0; see EMR note above.)

## IAM

The polyris SAM stack exports two managed policies via CloudFormation exports:

| Task type + usage | `PolyrisTaskReadPolicy` | `PolyrisTaskWritePolicy` |
|-------------------|:-----------------------:|:------------------------:|
| Lambda — `return value` only | not needed | not needed |
| Lambda — calls `xcom.get()` / `xcom.pull()` | **required** | not needed |
| `@task.sfn` — child SFN Output (no SDK call) | not needed | not needed |
| Glue / ECS / Batch / EMR — reads only via `xcom.get()` / `xcom.pull()` | **required** | not needed |
| Glue / ECS / Batch — calls `xcom.push()` | **required** | **required** |
| Athena — SQL only (no SDK call) | not needed | not needed |

Attach with `!ImportValue`:

```yaml
MyGlueTaskRole:
  Type: AWS::IAM::Role
  Properties:
    ManagedPolicyArns:
      - !ImportValue myorg-dev-polyris-task-read-policy
      - !ImportValue myorg-dev-polyris-task-write-policy   # only if calling xcom.push()
```

The write policy scopes `dynamodb:UpdateItem` to keys with `output#*` prefix — user tasks can't touch internal wrapper records (`_pause_*`, `_notify_warn_*`, `input#*`).

**Cross-account tasks** (task role in a different AWS account than the polyris deployment) cannot call `xcom.push()` without additional IAM setup — the `pipeline-tokens` table is in the polyris account. Set up a cross-account trust relationship or route through a Lambda proxy in the polyris account.

## Anti-patterns

| Don't | Do |
|-------|----|
| Pass 100k-row list of dicts through XCom | Write parquet to S3, return `{"path": "s3://..."}` |
| Rely on `event["upstream"]` for a Glue upstream without knowing about metadata leak | Read Console — banner warns; then add `xcom.push()` in the Glue job |
| `xcom.push()` from a Lambda handler | `return value` — deterministic ordering |
| Read `event["upstream"][X]["output"]["field"]` without checking status | `xcom.get(event, X)` — loud errors surface bugs earlier |
| Access raw `event["upstream"]` dict in new code | `xcom.get(event, X)` — auto-resolves `_s3_ref`, auto-falls-back on truncated |

## Where data lives (DDB schema)

Two records per task-per-day, both in the `pipeline-tokens` DDB table:

### `output#{pipeline}#{task}#{date}` — canonical output row

| Field | Type | Purpose |
|-------|------|---------|
| `execution_name` | S (PK) | `output#{pipeline}#{task}#{date}` |
| `task_name` | S | task_id |
| `pipeline_name` | S | pipeline identifier |
| `status` | S | `success` / `failed` / etc. |
| `result` | S (JSON) | task output (Lambda return, SFN Output, or `xcom.push()` value) |
| `updated_at` | S (ISO) | last update |
| `finished_at` | S (ISO) | completion timestamp |
| `ttl` | N | expiry (120 days) |
| `run_id` | S | most recent wrapper ARN that touched this row |
| `attempt` | N | current retry attempt |
| `task_execution_arn` | S | AWS resource ARN of the task execution |
| `_pushed_by_task` | BOOL | present only when `xcom.push()` ran this run |
| `pushed_at` | S (ISO) | when `xcom.push()` was called |
| `pushed_run_id` | S | wrapper ARN that owned the push (stale-marker rejection) |
| `push_count` | N (ADD) | number of push calls this run (debug) |

Init_Output_Row `REMOVE`s `_pushed_by_task`, `pushed_at`, `pushed_run_id` at the start of every run — stale markers from prior same-date runs never leak into `Check_Task_Pushed` decisions.

### `input#{pipeline}#{task}#{date}` — Console preview row (new in 0.100.0)

| Field | Type | Purpose |
|-------|------|---------|
| `execution_name` | S (PK) | `input#{pipeline}#{task}#{date}` |
| `task_name` | S | task_id |
| `pipeline_name` | S | pipeline identifier |
| `task_date` | S | task run date (**not** `date` — see GSI note) |
| `task_input` | S (JSON) | full `{upstream, variables}` snapshot up to ~380 KB |
| `updated_at` | S (ISO) | last update |
| `ttl` | N | expiry (120 days) |

**Note on `task_date`:** deliberately mismatched from the `date-pipeline-index` GSI's key attribute (`date`) so `input#` rows don't populate that GSI. `is_internal_record()` also filters `input#*` by prefix as belt-and-suspenders.

## Determinism

Data flows are keyed on the logical run date, not wall-clock time. Both `event.upstream` and `xcom.get()` / `xcom.pull()` are deterministic and safe to re-run — as long as your task's return value doesn't include `now()` or similar wall-clock inputs.

Same-date re-runs overwrite; different dates never collide. TTL is 120 days.
