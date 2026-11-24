# Passing data between tasks

**Reference.** Task A returns some data. Task B reads it. This page pins the
API surface, error taxonomy, DDB shape, and IAM contract for every task type
polyris supports. For a step-by-step introduction, start with the
[`16_xcom_showcase` example](../../examples/16_xcom_showcase/); for the
end-to-end design rationale, see
[ADR #123](../reference/adr-123-xcom-reliable-data-passing.md).

## Requires

`polyris >= 1.3.0` in your task deployment bundles: a Lambda's package (`polyris-build-lambda`), a Glue job's wheel (`--extra-py-files`), an ECS or Batch image. Rebuild them when you upgrade to 1.3. An older copy of `xcom` returns `None` for an upstream whose output the task's input could not hold (a fan-in over about 1,500 upstreams). It also raises for an upstream the run skipped, and can read an older run's output of the same date after an upstream failed.

## Contents

- [Cheat sheet — one call, every task type](#cheat-sheet--one-call-every-task-type)
- [Reading upstream: `xcom.get()`](#reading-upstream-xcomget)
- [Writing output](#writing-output)
- [Legacy reader: `xcom.pull()`](#legacy-reader-xcompull)
- [Failure taxonomy](#failure-taxonomy)
- [`XComManuallyResolvedError` — reading the marker for diagnostics](#xcommanuallyresolvederror--reading-the-marker-for-diagnostics)
- [The service task metadata trap](#the-service-task-metadata-trap)
- [Lambda write pattern](#lambda-write-pattern)
- [Size limits](#size-limits)
- [Large outputs — Claim Check pattern](#large-outputs--claim-check-pattern)
- [Installing polyris SDK in Glue / ECS / Batch / EMR](#installing-polyris-sdk-in-glue--ecs--batch--emr)
- [IAM](#iam)
- [Anti-patterns](#anti-patterns)
- [Where data lives (DDB schema)](#where-data-lives-ddb-schema)
- [Determinism](#determinism)
- [Migration from 0.99](#migration-from-099)

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

That's the whole mental model. The rest of this page is the specification.

## Reading upstream: `xcom.get()`

```python
xcom.get(event, task_name, *,
         raise_on_missing=True,     # dep with no output in this run → XComMissingError
         raise_on_failure=True,     # upstream failed finally → XComUpstreamFailedError
         raise_on_manual=True,      # upstream manually resolved via UI → XComManuallyResolvedError
         ddb_client=None,           # injected for tests
         s3_client=None)            # injected for tests
```

**What a task reads of its upstreams.** A task reads an upstream's output only
when that upstream succeeded in this run, or an operator or the run skipped it.
A skip by the run (`skip_tasks`) keeps the output an earlier run of the same date
wrote. Any other upstream reaches the task as its status alone, so a task that
runs after a failure never reads an older run's output of the same date:

- After a final failure (`failed`, `upstream_failed`, `stopped`), `get()` raises
  `XComUpstreamFailedError`, or returns `None` with `raise_on_failure=False`.
- After a skip by a trigger rule, `get()` raises `XComMissingError`, or returns
  `None` with `raise_on_missing=False`.
- Beside an upstream that has not succeeded yet in this run (it still waits or
  runs when a `one_success` or `one_failed` task starts), `get()` raises
  `XComMissingError` naming the status, or returns `None` with
  `raise_on_missing=False`.

A task's result is stored as JSON and reaches its dependants as the value the
task returned: a string as that string (stored with its quotes, `"ok"`). A
string of JSON text (`json.dumps(...)`) reaches them parsed, as in 1.2. A
string result that an older version stored as it was reaches them as that
string.

An upstream that succeeded always has its output, because the output is written
together with the success. If it is missing anyway, the task fails with that
cause before its work starts, and waits for an operator's decision.

A Lambda or `@task.sfn` task gets each upstream in `event["upstream"][name]`:
its `status`, its `skip_origin`, and its `output` when the task reads it. An ECS
or Batch task gets the statuses in the environment variable
`POLYRIS_UPSTREAM_STATUSES`, beside `POLYRIS_TASK_NAME`. A Glue job gets them as
the job argument `--POLYRIS_UPSTREAM_STATUSES`; `xcom` reads it, and every other
`--POLYRIS_*` argument, from the job's command line, so the script copies none
of them into the environment. An EMR step gets neither: after an upstream failed, `xcom`
in an EMR step can return an older run's output of the same date.

**Lookup order:**

1. If `event.upstream[task_name]` exists (Lambda / SFN pre-fetched inject) → use it.
   Auto-resolves `{"_s3_ref": "s3://..."}` pointers. On a `{"_truncated": true}`
   marker, or an entry without `output`, falls back to DDB automatically (see
   [Size limits](#size-limits)).
2. Otherwise → reads DDB directly via `pull()`. Works with `event=None` for
   Glue / ECS / Batch / EMR. A Glue, ECS or Batch task checks the upstream's
   status in `POLYRIS_UPSTREAM_STATUSES` first.

**Optional upstream** with `trigger_rule="all_done"`:

```python
def handler(event, context):
    sales = xcom.get(event, "extract_sales")                                # required — raises if missing
    bonus = xcom.get(event, "bonus_source", raise_on_failure=False) or {}   # optional — None if failed
```

## Writing output

### Lambda / `@task.sfn` — return your value

`run_task` captures the Lambda return value or the child SFN's `Output`. No
SDK call needed. This is the idiomatic path.

```python
def handler(event, context):
    return {"rows": 100, "path": "s3://..."}
```

**Do not** call `xcom.push()` from a Lambda handler — see
[Lambda write pattern](#lambda-write-pattern).

### Glue / ECS / Batch — call `xcom.push()`

Service tasks run in their own containers / jobs — `run_task` only sees the
AWS API response (`{JobRunId}`, `{TaskArn}`, `{JobId}`), not the work output.
`xcom.push()` writes the real value directly to DDB.

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

The push writes to the same DDB key `run_task` writes on success
(`output#{pipeline}#{task}#{date}`). `run_task` detects a `_pushed_by_task`
marker with a matching `pushed_run_id` and skips overwriting the pushed value
with the AWS API response.

### EMR and Athena — reads yes, `xcom.push()` no

Both task types can **read** with `xcom.get(None, "upstream")` from job code
(subject to installing the SDK on the cluster / not applicable for Athena SQL)
and to holding `PolyrisTaskReadPolicy`. Neither can call `xcom.push()`:

- **EMR** — `addStep.sync` has no `Environment` field to inject
  `POLYRIS_TASK_NAME` etc., and injecting via `HadoopJarStep.Args` would risk
  breaking user Spark arg parsers.
- **Athena** — SQL can't execute arbitrary Python. Two patterns:
  - Values known at deploy time → interpolate them into `query_string` with a Python f-string. polyris passes the query to Athena as it is, and the decorator refuses `{{ ... }}` and `{% ... %}` in it.
  - Otherwise → put a Lambda after Athena. The Lambda reads the query's id from `event["upstream"]["athena_task"]["output"]["QueryExecution"]["QueryExecutionId"]`,
    resolves the query result, and returns / pushes the actual value.

To pass a value from an EMR / Athena task to downstream tasks, use one of the
Lambda-front or claim-check patterns above.

## Legacy reader: `xcom.pull()`

```python
xcom.pull(task_name, context=None, *,
          pipeline=None, date=None, table=None,
          raise_on_manual=True,
          ddb_client=None, s3_client=None)
```

Low-level DDB reader — hits `output#{pipeline}#{task}#{date}` directly with no
event-inject shortcut. Kept for pre-1.0.0 code and for callers that need to
read a specific date / pipeline explicitly (out-of-band tooling). New code
should prefer `xcom.get()`, which layers the manual-resolution guard, the
upstream-failure guard, and inject → DDB fallback on top of the same reader.

**Behaviour differences from `get()`:**

- No `raise_on_missing` / `raise_on_failure` — missing rows raise
  `XComMissingError` (aliased as `PullError`) unconditionally. The upstream's
  status is checked as `get()` checks it: from the event in a Lambda
  (`xcom.pull(name, event)`), from `POLYRIS_UPSTREAM_STATUSES` in Glue, ECS or
  Batch. An upstream that failed finally raises `XComUpstreamFailedError`, and
  any other upstream without an output in this run `XComMissingError`; use
  `get()` to receive `None` instead.
- Truncated rows raise `XComMissingError` (via `PullError`), not
  `XComTruncatedError`. `get()` upgrades the error type when it detects the
  truncation marker.
- `raise_on_manual` works the same way (default `True`; set `False` to receive
  the marker dict).

## Failure taxonomy

Every reader error inherits `XComError`, which inherits `RuntimeError`.

| Error | Raised when | Suppress with |
|-------|-------------|---------------|
| `XComMissingError` | no output stored (dep didn't run, a trigger rule skipped it, or it returned nothing) | `raise_on_missing=False` — returns `None` |
| `XComUpstreamFailedError` | upstream failed finally in this run: `failed`, `upstream_failed` or `stopped`; its `status` attribute says which | `raise_on_failure=False` — returns `None` |
| `XComTruncatedError` | both the event inject and the DDB row came back truncated (`get()` only) | not suppressible — see [Claim Check pattern](#large-outputs--claim-check-pattern) |
| `XComManuallyResolvedError` | upstream carries a Console-written manual-resolution marker | `raise_on_manual=False` — returns the marker dict as-is |
| `PullError` | back-compat alias for `XComMissingError` — existing `except PullError:` code continues to work | same as `XComMissingError` |

**Ordering when multiple opt-outs interact.** In `xcom.get()` the checks fire
in this exact order for a dep that exists in `event.upstream`:

1. **Failure check** — a final failure → `XComUpstreamFailedError` (subject to
   `raise_on_failure`); a skip by a trigger rule → `XComMissingError` (subject
   to `raise_on_missing`). Neither has an output in this run.
2. **Manual-resolution check** — `_is_manual_marker(output)` →
   `XComManuallyResolvedError` (subject to `raise_on_manual`). A Mark Successful
   or a Skip by an operator leaves the marker as the upstream's output.
3. **Truncation** → transparent DDB fallback → `XComTruncatedError` if the DDB
   row is also truncated.

**Concrete matrix — an upstream the operator resolved:**

| Operator's action | Default | `raise_on_manual=False` | `raise_on_failure=False` |
|-------------------|---------|-------------------------|--------------------------|
| Mark Successful, Skip | `XComManuallyResolvedError` | returns the marker dict (`{"_manually_resolved": True, "_resolution": "skip", …}`) | `XComManuallyResolvedError` |
| Fail, Stop | `XComUpstreamFailedError` | `XComUpstreamFailedError` | returns `None` |

## `XComManuallyResolvedError` — reading the marker for diagnostics

When you catch a `XComManuallyResolvedError`, four attributes are populated on
the exception; the fifth (`pipeline_execution`) is present when the marker was
written by 1.0.0 or later:

```python
except XComManuallyResolvedError as e:
    e.task_name           # the upstream task_id
    e.resolution          # "mark_success" | "skip" | "fail" | "stop"
    e.operator            # operator email (or "unknown" if auth disabled / legacy)
    e.reason              # free-text reason the operator typed
    e.pipeline_execution  # source pipeline_execution — see below
```

The `pipeline_execution` field names the run in which the operator wrote the
marker. It matters because `output#{pipeline}#{task}#{date}` is **date-scoped,
not run-scoped** — a manual resolution on run A leaves the marker in place for
same-date run B until run B writes an organic output. If a downstream task in
run B raises `XComManuallyResolvedError` with `e.pipeline_execution != current
run`, it's cross-run bleed, not a fresh operator action. Filter and route
accordingly:

```python
except XComManuallyResolvedError as e:
    if e.pipeline_execution and e.pipeline_execution != event.get("pipeline_execution"):
        log.warn(f"cross-run marker from {e.pipeline_execution}; skipping")
        return
    # Fresh manual action — route on the operator's intent:
    if e.resolution == "skip":
        return  # respect the operator's skip
    raise
```

**To introspect the marker without an exception** (rare — routes on the
operator's intent as data, not a control-flow event):

```python
marker = xcom.get(event, "extract_sales", raise_on_manual=False)
if isinstance(marker, dict) and marker.get("_manually_resolved"):
    resolution = marker["_resolution"]      # "mark_success" | "skip" | "fail" | "stop"
    operator   = marker.get("_operator", "unknown")
    reason     = marker.get("_reason", "")
    pipeline_exec = marker.get("_pipeline_execution", "")
```

## The service task metadata trap

Without `xcom.push()`, a service task's stored `result` is the AWS API
response, not application data. Downstream sees:

| Task type | Stored `result` without push |
|-----------|------------------------------|
| `lambda_function` | Lambda return value (real data — no push needed) |
| `sfn` | Child SFN Output (real data — no push needed) |
| `glue_job` | `{"JobRunId": ...}` |
| `ecs_task` | `{"Tasks": [{"TaskArn": ...}]}` |
| `athena_query` | `{"QueryExecution": {"QueryExecutionId": ...}}` |
| `batch_job` | `{"JobId": ...}` |
| `emr_step` | AWS response with step identifier (no push option — see EMR note) |

Console flags this: when the Output tab detects an AWS-response shape, a banner
appears with the hint to call `xcom.push()` (or, for EMR / Athena, to switch
to a Lambda front).

## Lambda write pattern

`xcom.push()` from a Lambda handler races with `run_task`'s success write
(`Record_Success`). If the push completes before the handler returns, it's fine; if it
happens asynchronously after return, ordering is undefined and downstream may
see the wrong value.

- Prefer `return value` from Lambda — `run_task` captures it deterministically.
- `xcom.push()` from a Lambda emits a `UserWarning` explaining this.
- If you call both `xcom.push({A})` and `return {B}`, the pushed value wins
  (`run_task` detects the marker and doesn't overwrite `result`). Pick one;
  don't mix.

## Size limits

| Constraint | Limit | Fix at the limit |
|-----------|-------|------------------|
| Runtime inject per dep | 25 KB, and all of a task's upstreams share about 200 KB with its input | Use `xcom.get()` — auto-falls-back to DDB (~350 KB). |
| DDB `result` field | 350 KB | Use [Claim Check pattern](#large-outputs--claim-check-pattern). |
| Console preview (`task_input`) | ~380 KB | New in 1.0.0 — separate `input#` DDB record. |
| DDB item hard limit (AWS) | 400 KB | AWS constraint. |

For >350 KB payloads, use the Claim Check pattern.

## Large outputs — Claim Check pattern

Real data pipelines write big values to S3 as parquet / JSON with a proper
schema, and XCom carries a pointer, not the data itself. If a task genuinely
needs to hand off a large blob via XCom, use the manual claim-check convention:

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

Bring your own bucket. Bring your own IAM (`s3:PutObject` for the writer,
`s3:GetObject` for the reader). The polyris-managed `PolyrisTaskReadPolicy`
grants only DDB read, not S3 read on user buckets.

## Installing polyris SDK in Glue / ECS / Batch / EMR

`xcom.get()` / `xcom.push()` live in the `polyris` package. Your task code
needs to import it. Install it directly from a git tag.

> Replace `<VERSION>` in every snippet below with a real git tag from
> [github.com/Polyris/polyris/tags](https://github.com/Polyris/polyris/tags)
> (e.g. `v1.0.1`). Never pin to `main` in production.

**Glue (Spark ETL and Python Shell)** — Glue does not install a git URL reliably: `--additional-python-modules` splits `polyris @ git+https://...` on its spaces and the job fails at launch. Bake polyris into a wheel and pass it via `--extra-py-files`, which both job types accept:

```yaml
DefaultArguments:
  "--extra-py-files": "s3://<your-bucket>/wheels/polyris-<VERSION>-py3-none-any.whl"
```

Build the wheel once from the git tag: `pip wheel --no-deps "polyris @ git+https://github.com/Polyris/polyris@<VERSION>" -w dist/` then `aws s3 cp dist/polyris-*.whl s3://<your-bucket>/wheels/`.

**ECS / Batch** — install into your container image:

```dockerfile
FROM python:3.12
RUN pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"
```

`python:3.12` (full, not `slim`) has `git` bundled — needed for git+URL
install. Use `-slim` only if you download the wheel from S3 in the container
entrypoint instead.

**Lambda** — two options:

*Option A — `polyris-build-lambda` (no pip in the zip build):*

```bash
polyris-build-lambda ./my_lambda_dir/
```

Stamps `polyris/xcom.py` + `polyris/__init__.py` into the handler directory.
`from polyris import xcom` works in the deployed function without any pip step.
Re-run after a polyris upgrade to refresh the snapshot. Pass `--force` to
overwrite an existing `polyris/` directory.

*Option B — `requirements.txt` (standard pip approach):*

Add one line to your Lambda's `requirements.txt`:
```
polyris @ git+https://github.com/Polyris/polyris@<VERSION>
```
Then `pip install -r requirements.txt -t .` before zipping.

**EMR** — bootstrap script:

```bash
sudo pip install "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"
```

Reads work; writes via `xcom.push()` are not supported (see
[EMR and Athena — reads yes, `xcom.push()` no](#emr-and-athena--reads-yes-xcompush-no)).

## IAM

The polyris SAM stack exports two managed policies via CloudFormation exports:

| Task type + usage | `PolyrisTaskReadPolicy` | `PolyrisTaskWritePolicy` |
|-------------------|:-----------------------:|:------------------------:|
| Lambda — `return value` only | not needed | not needed |
| Lambda — calls `xcom.get()` / `xcom.pull()` | **required** | not needed |
| `@task.sfn` — child SFN Output (no SDK call) | not needed | not needed |
| Glue / ECS / Batch — reads only via `xcom.get()` / `xcom.pull()` | **required** | not needed |
| Glue / ECS / Batch — calls `xcom.push()` | **required** | **required** |
| EMR — reads only via `xcom.get()` / `xcom.pull()` (writes not supported) | **required** | not needed |
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

The write policy scopes `dynamodb:UpdateItem` to keys with `output#*` prefix
— user tasks can't touch the platform's own records (`_run_*`,
`_notify_warn_*`, `input#*`).

**Cross-account tasks** (task role in a different AWS account than the polyris
deployment) cannot call `xcom.push()` without additional IAM setup — the
`pipeline-tokens` table is in the polyris account. Set up a cross-account
trust relationship or route through a Lambda proxy in the polyris account.

## Anti-patterns

| Don't | Do |
|-------|----|
| Pass 100k-row list of dicts through XCom | Write parquet to S3, return `{"path": "s3://..."}` |
| Rely on `event["upstream"]` for a Glue upstream without knowing about metadata leak | Read Console — banner warns; then add `xcom.push()` in the Glue job |
| `xcom.push()` from a Lambda handler | `return value` — deterministic ordering |
| Read `event["upstream"][X]["output"]["field"]` without checking status | `xcom.get(event, X)` — loud errors surface bugs earlier |
| Access raw `event["upstream"]` dict in **new** code | `xcom.get(event, X)` — auto-resolves `_s3_ref`, auto-falls-back on truncated. Existing readers keep working — see [Migration from 0.99](#migration-from-099). |
| Read a `XComManuallyResolvedError` marker as if it were data | Catch the error; route on `.resolution` / `.operator` / `.reason` |

## Where data lives (DDB schema)

Two records per task-per-day, both in the `pipeline-tokens` DDB table:

### `output#{pipeline}#{task}#{date}` — canonical output row

| Field | Type | Purpose |
|-------|------|---------|
| `execution_name` | S (PK) | `output#{pipeline}#{task}#{date}` |
| `task_name` | S | task_id |
| `status` | S | `success`, written with the output |
| `result` | S (JSON) | the task's output: a Lambda's return, a state machine's output, another service's response, or the `xcom.push()` value; a string that is not JSON text is stored as a JSON string (`"ok"`) |
| `updated_at` | S (ISO) | last write |
| `run_id` | S | the `run_task` execution of the latest success |
| `ttl` | N | a year from the latest write; every write renews it |
| `_pushed_by_task` | BOOL | present when `xcom.push()` wrote `result` |
| `pushed_at` | S (ISO) | when `xcom.push()` ran |
| `pushed_run_id` | S | the `run_task` execution whose task pushed |
| `push_count` | N (ADD) | number of push calls (debug) |
| `_manually_resolved` | BOOL | present when the Console wrote a manual-resolution marker (see below) |
| `_resolution` | S | `mark_success` \| `skip` \| `fail` \| `stop` |
| `_reason` | S | operator's free-text reason |
| `_operator` | S | operator email (or `"unknown"` if auth disabled / legacy) |
| `_pipeline_execution` | S | source pipeline_execution the marker was written from |

`run_task` writes `result` in the same transaction as the task's success. A success whose task pushed keeps the pushed value; one that did not push removes the push fields. `pushed_run_id` keeps a push of an earlier run of the date from counting as this run's.

### `input#{pipeline}#{task}#{date}` — Console preview row

| Field | Type | Purpose |
|-------|------|---------|
| `execution_name` | S (PK) | `input#{pipeline}#{task}#{date}` |
| `task_name` | S | task_id |
| `pipeline_name` | S | pipeline identifier |
| `task_date` | S | task run date (**not** `date` — see GSI note) |
| `task_input` | S (JSON) | the `{upstream, variables}` the task's work was given |
| `run_id` | S | the `run_task` execution that wrote it |
| `updated_at` | S (ISO) | last write |
| `ttl` | N | 90 days from the latest write |

**Note on `task_date`:** deliberately mismatched from the `date-pipeline-index`
GSI's key attribute (`date`) so `input#` rows don't populate that GSI.
`is_internal_record()` also filters `input#*` by prefix as belt-and-suspenders.

## Determinism

Data flows are keyed on the logical run date, not wall-clock time. Both
`event.upstream` and `xcom.get()` / `xcom.pull()` are deterministic and safe
to re-run — as long as your task's return value doesn't include `now()` or
similar wall-clock inputs.

Same-date re-runs overwrite; different dates never collide. A date's output lives a year from its latest write, its input 90 days.

## Migration from 0.99

If you're upgrading a pipeline from 0.99 or earlier, three changes matter:

1. **Reader API.** Existing readers keep working — nothing to change on day
   one:
   - `event["upstream"][X]["output"]` — still populated by `run_task`.
   - `xcom.pull(X, event)` — same signature, same return shape.
   - `except PullError` — still catches missing rows (`PullError` is now an
     alias for `XComMissingError`).

   When you're ready to adopt the new guards, replace call-sites with
   `xcom.get(event, X)`. The new call adds: upstream-status check
   (`raise_on_failure`), manual-resolution guard (`raise_on_manual`), and
   transparent inject → DDB fallback on truncation.

2. **Writer API for service tasks.** Before 1.0.0, Glue / ECS / Batch tasks
   silently stored the AWS API response as `result` — downstream received
   `{"JobRunId": ...}` instead of the real work output. In 1.0.0, call
   `xcom.push({...})` before the job ends to overwrite the metadata with the
   actual payload. The Console shows a warning banner on tasks whose stored
   result looks like AWS metadata.

3. **New error types.** Existing `except PullError:` code continues to work.
   New code should catch the specific `XCom*Error` type it's handling —
   catching `XComError` covers all four.

Deploy order: install the new SDK in your task bundles first
(`polyris @ git+https://github.com/Polyris/polyris@<VERSION>` — see the
[install section above](#installing-polyris-sdk-in-glue--ecs--batch--emr)),
then redeploy the pipeline. Polyris 1.3 needs every bundle rebuilt with its
own SDK (see *Breaking* in the [CHANGELOG](../../CHANGELOG.md)).
