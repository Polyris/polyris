# Passing data between tasks

**Reference.** Task A returns some data. Task B reads it. This page pins the
API surface, error taxonomy, DDB shape, and IAM contract for every task type
polyris supports. For a step-by-step introduction, start with the
[`16_xcom_showcase` example](../../examples/16_xcom_showcase/); for the
end-to-end design rationale, see
[ADR #123](../reference/adr-123-xcom-reliable-data-passing.md).

## Requires

`polyris >= 1.0.0` in your task deployment bundles (Lambda zip, Glue
`--additional-python-modules`, ECS container install, etc.). The `xcom.get()`
/ `xcom.push()` API and the typed `XCom*Error` hierarchy ship in that version.

Older SDK versions keep working with the legacy
`event["upstream"][X]["output"]` / `xcom.pull()` patterns — they just don't get
the new typed errors, transparent truncation fallback, or the manual-resolution
guard. See [Migration from 0.99](#migration-from-099).

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
         raise_on_missing=True,     # dep with no output → XComMissingError
         raise_on_failure=True,     # upstream status != success → XComUpstreamFailedError
         raise_on_manual=True,      # upstream manually resolved via UI → XComManuallyResolvedError
         ddb_client=None,           # injected for tests
         s3_client=None)            # injected for tests
```

**Lookup order:**

1. If `event.upstream[task_name]` exists (Lambda / SFN pre-fetched inject) → use it.
   Auto-resolves `{"_s3_ref": "s3://..."}` pointers. On a `{"_truncated": true}`
   marker, falls back to DDB automatically (25 KB inject cap vs 350 KB DDB row).
2. Otherwise → reads DDB directly via `pull()`. Works with `event=None` for
   Glue / ECS / Batch / EMR.

**Optional upstream** with `trigger_rule="all_done"`:

```python
def handler(event, context):
    sales = xcom.get(event, "extract_sales")                                # required — raises if missing
    bonus = xcom.get(event, "bonus_source", raise_on_failure=False) or {}   # optional — None if failed
```

## Writing output

### Lambda / `@task.sfn` — return your value

The wrapper captures the Lambda return value or the child SFN's `Output`. No
SDK call needed. This is the idiomatic path.

```python
def handler(event, context):
    return {"rows": 100, "path": "s3://..."}
```

**Do not** call `xcom.push()` from a Lambda handler — see
[Lambda write pattern](#lambda-write-pattern).

### Glue / ECS / Batch — call `xcom.push()`

Service tasks run in their own containers / jobs — the wrapper only sees the
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

The push writes to the same DDB key the wrapper writes on success
(`output#{pipeline}#{task}#{date}`). The wrapper detects a `_pushed_by_task`
marker with a matching `pushed_run_id` and skips overwriting the pushed value
with the AWS API response.

### EMR and Athena — reads yes, `xcom.push()` no

Both task types can **read** with `xcom.get(None, "upstream")` from job code
(subject to installing the SDK on the cluster / not applicable for Athena SQL)
and to holding `PolyrisTaskReadPolicy`. Neither can call `xcom.push()`:

- **EMR** — `addStep.sync` has no `Environment` field to inject
  `POLYRIS_TASK_NAME` etc., and injecting via `HadoopJarStep.Args` would risk
  breaking user Spark arg parsers. Deferred to a follow-up.
- **Athena** — SQL can't execute arbitrary Python. Two patterns:
  - Small values → set them on `variables=` at the DAG level and reference
    from the query via templating.
  - Otherwise → put a Lambda after Athena. The Lambda reads Athena's result
    location from `event.upstream["athena_task"]["output"]["QueryExecutionId"]`,
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
  `XComMissingError` (aliased as `PullError`) unconditionally; the row's status
  is not consulted.
- Truncated rows raise `XComMissingError` (via `PullError`), not
  `XComTruncatedError`. `get()` upgrades the error type when it detects the
  truncation marker.
- `raise_on_manual` works the same way (default `True`; set `False` to receive
  the marker dict).

## Failure taxonomy

Every reader error inherits `XComError`, which inherits `RuntimeError`.

| Error | Raised when | Suppress with |
|-------|-------------|---------------|
| `XComMissingError` | no output stored (dep didn't run, was skipped, or returned nothing) | `raise_on_missing=False` — returns `None` |
| `XComUpstreamFailedError` | upstream status is `skipped`, `failed`, `aborted`, etc. (`get()` only) | `raise_on_failure=False` — returns whatever output was recorded |
| `XComTruncatedError` | both the event inject and the DDB row came back truncated (`get()` only) | not suppressible — see [Claim Check pattern](#large-outputs--claim-check-pattern) |
| `XComManuallyResolvedError` | upstream carries a Console-written manual-resolution marker | `raise_on_manual=False` — returns the marker dict as-is |
| `PullError` | back-compat alias for `XComMissingError` — existing `except PullError:` code continues to work | same as `XComMissingError` |

**Ordering when multiple opt-outs interact.** In `xcom.get()` the checks fire
in this exact order for a dep that exists in `event.upstream`:

1. **Missing check** — status `"unknown"` → `XComMissingError` (subject to
   `raise_on_missing`).
2. **Manual-resolution check** — `_is_manual_marker(output)` →
   `XComManuallyResolvedError` (subject to `raise_on_manual`). This runs
   *before* the status check because a human's Mark success / Skip / Fail /
   Stop is more informative than the status the action produced.
3. **Failure check** — `status != "success"` → `XComUpstreamFailedError`
   (subject to `raise_on_failure`).
4. **Truncation** → transparent DDB fallback → `XComTruncatedError` if the DDB
   row is also truncated.

**Concrete matrix — the four `raise_on_manual` × `raise_on_failure` combinations
on a task the operator manually failed** (status = `failed`, output = marker):

| `raise_on_manual` | `raise_on_failure` | Result |
|-------------------|-------------------|--------|
| `True` (default) | `True` (default) | `XComManuallyResolvedError` — manual check wins |
| `True` (default) | `False` | `XComManuallyResolvedError` — manual check wins |
| `False` | `True` (default) | `XComUpstreamFailedError` — marker skipped, status check fires |
| `False` | `False` | returns the marker dict (`{"_manually_resolved": True, "_resolution": "fail", …}`) |

Silencing `raise_on_manual` alone still surfaces the failed status via
`XComUpstreamFailedError` — you have to silence both to receive the marker
verbatim.

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

`xcom.push()` from a Lambda handler races with the wrapper's `Save_Success`
write. If the push completes before the handler returns, it's fine; if it
happens asynchronously after return, ordering is undefined and downstream may
see the wrong value.

- Prefer `return value` from Lambda — the wrapper captures it deterministically.
- `xcom.push()` from a Lambda emits a `UserWarning` explaining this.
- If you call both `xcom.push({A})` and `return {B}`, the pushed value wins
  (the wrapper detects the marker and doesn't overwrite `result`). Pick one;
  don't mix.

## Size limits

| Constraint | Limit | Fix at the limit |
|-----------|-------|------------------|
| Runtime inject per dep | 25 KB | Use `xcom.get()` — auto-falls-back to DDB (~350 KB). |
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
needs to import it. Polyris is not yet on PyPI (planned but not scheduled),
so the install string is a git-tag URL rather than a plain PyPI name.

> Replace `<VERSION>` in every snippet below with a real git tag from
> [github.com/Polyris/polyris/tags](https://github.com/Polyris/polyris/tags)
> (e.g. `v1.0.1`). Never pin to `main` in production.

**Glue Spark ETL (`glueetl`)** — `--additional-python-modules` (accepts
git+URL because it's a pip wrapper):

```yaml
DefaultArguments:
  "--additional-python-modules": "polyris @ git+https://github.com/Polyris/polyris@<VERSION>"
```

**Glue Python Shell** — `--additional-python-modules` does NOT accept
git+URL reliably on Python Shell jobs (see `polyris/CLAUDE.md` "Glue Python
install" rule). Bake polyris into a wheel and pass via `--extra-py-files`:

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

**Lambda** — ship in the deployment zip. `requirements.txt` one-liner:
`polyris @ git+https://github.com/Polyris/polyris@<VERSION>`.

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
— user tasks can't touch internal wrapper records (`_pause_*`,
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
| Access raw `event["upstream"]` dict in new code | `xcom.get(event, X)` — auto-resolves `_s3_ref`, auto-falls-back on truncated |
| Read a `XComManuallyResolvedError` marker as if it were data | Catch the error; route on `.resolution` / `.operator` / `.reason` |

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
| `_manually_resolved` | BOOL | present when the Console wrote a manual-resolution marker (see below) |
| `_resolution` | S | `mark_success` \| `skip` \| `fail` \| `stop` |
| `_reason` | S | operator's free-text reason |
| `_operator` | S | operator email (or `"unknown"` if auth disabled / legacy) |
| `_pipeline_execution` | S | source pipeline_execution the marker was written from |

Init_Output_Row `REMOVE`s `push_count` at the start of every run so the counter
resets per-run. Other per-run markers (`_pushed_by_task`, `pushed_at`,
`pushed_run_id`) are overwritten by `Save_Canonical_Output_Preserve` /
`Check_Task_Pushed`; the `run_id` stamp lets same-date runs distinguish their
own writes from a prior run's leftovers.

### `input#{pipeline}#{task}#{date}` — Console preview row (new in 1.0.0)

| Field | Type | Purpose |
|-------|------|---------|
| `execution_name` | S (PK) | `input#{pipeline}#{task}#{date}` |
| `task_name` | S | task_id |
| `pipeline_name` | S | pipeline identifier |
| `task_date` | S | task run date (**not** `date` — see GSI note) |
| `task_input` | S (JSON) | full `{upstream, variables}` snapshot up to ~380 KB |
| `run_id` | S | wrapper ARN that wrote this input snapshot |
| `updated_at` | S (ISO) | last update |
| `ttl` | N | expiry (120 days) |

**Note on `task_date`:** deliberately mismatched from the `date-pipeline-index`
GSI's key attribute (`date`) so `input#` rows don't populate that GSI.
`is_internal_record()` also filters `input#*` by prefix as belt-and-suspenders.

## Determinism

Data flows are keyed on the logical run date, not wall-clock time. Both
`event.upstream` and `xcom.get()` / `xcom.pull()` are deterministic and safe
to re-run — as long as your task's return value doesn't include `now()` or
similar wall-clock inputs.

Same-date re-runs overwrite; different dates never collide. TTL is 120 days.

## Migration from 0.99

If you're upgrading a pipeline from 0.99 or earlier, three changes matter:

1. **Reader API.** Existing readers keep working — nothing to change on day
   one:
   - `event["upstream"][X]["output"]` — still populated by the wrapper.
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
then redeploy the pipeline so the wrapper picks up the new `input#` record
contract. The wrapper is backward-compatible with pre-1.0.0 task bundles
(they just don't call `xcom.push()`).
