# Passing data between tasks

Task A returns some data. Task B reads it. This page shows how, per task type,
in as little code as possible.

## Cheat sheet — how each task type reads upstream

**`@task.lambda_function`** — arrives in the handler `event`, no setup needed:

```python
def load(event):
    rows = event["upstream"]["extract"]["output"]["rows"]
```

**`@task.sfn`** — arrives in `$states.input`, read via JSONata:

```json
"Arguments": "{% $states.input.upstream.extract.output.rows %}"
```

**`@task.glue_job` / `@task.ecs_task` / `@task.batch_job` / `@task.emr_step`** —
call `xcom.pull()` inside the job/container code:

```python
from polyris import xcom

data = xcom.pull("extract")     # {"rows": 1240, ...}
rows = data["rows"]
```

**`@task.athena_query`** — SQL can't call `xcom.pull()`. Two workable patterns:

- Small values → put in `variables={...}` on the DAG and reference from the
  query string.
- Otherwise → put a Lambda in front that pulls and either builds the SQL or
  calls Athena directly.

## The two paths

- **Lambda + SFN** — polyris injects upstream output into their input. Zero
  setup, no IAM, no `pull()`. Practical cap: ~25 KB per upstream output
  (Step Functions payload divides ~256 KB across inputs, so plan around
  25 KB when you have several upstreams).
- **Service tasks (Glue / ECS / Batch / EMR)** — polyris cannot inject
  arbitrary data into a Spark session / container / JVM. Your job code
  calls `xcom.pull()`. Cap: ~350 KB per output.

That's the entire mental model.

## Full example — Lambda → Glue

Lambda extracts, Glue aggregates. Both talk to xcom, in the two different
ways from the cheat sheet:

```python
from polyris import DAG, task

with DAG(
    dag_id="sales-etl",
    schedule="@daily",
    variables={"batch_size": 500},     # pipeline-wide, resolved at run start
) as dag:

    @task.lambda_function(
        function_name="sales-extract",
        payload={"source": "internal-crm"},   # static task input
    )
    def extract(event):
        # event has: your payload, upstream outputs (none here),
        # polyris metadata (date, pipeline_name, ...), pipeline variables.
        source = event["source"]                  # from payload=
        date   = event["date"]                    # polyris metadata
        batch  = event["batch_size"]              # pipeline variable

        # ... run the extract ...

        return {                                  # this is what Glue will pull
            "rows": 1240,
            "path": f"s3://lake/sales/{date}/data.parquet",
        }

    @task.glue_job(job_name="sales-aggregate")
    def aggregate():
        # Actual code runs on Spark, not in this file. It would do:
        #
        #     from polyris import xcom
        #     up = xcom.pull("extract")   # {"rows": 1240, "path": "..."}
        #     rows_path = up["path"]
        pass

    extract() >> aggregate()
```

## What actually gets stored, per task type

**Read this before you plan a service task → something else hand-off.** The
wrapper stores different things depending on task type:

| Task type              | Stored `result`                                         |
| ---------------------- | ------------------------------------------------------- |
| `lambda_function`      | Your Lambda's **return value** (JSON)                   |
| `sfn`                  | The nested SFN's **execution Output** (JSON)            |
| `glue_job`             | AWS API response — `{"JobRunId": ...}`                  |
| `ecs_task`             | AWS API response — `{"TaskArn": ...}`                   |
| `athena_query`         | AWS API response — `{"QueryExecutionId": ...}`          |
| `batch_job`            | AWS API response — `{"JobId": ...}`                     |
| `emr_step`             | AWS API response — step identifier                      |

So `xcom.pull("my-lambda")` returns your real data.
`xcom.pull("my-glue-job")` returns `{"JobRunId": "..."}`, not your rows.

**To pass real data FROM a service task, write it to S3:**

```python
# Inside the Glue job (Spark):
df.write.parquet(f"s3://lake/etl/{polyris_date}/data.parquet")

# In a downstream Lambda:
def load(event):
    path = f"s3://lake/etl/{event['date']}/data.parquet"
    df = pandas.read_parquet(path)
```

There is no `xcom.push()` — service tasks can't write anything but the AWS API
response through the wrapper. S3 by convention is the way.

## What's in a task's input

Every task input carries four pieces. Where you read each depends on task type:

| Piece                     | Lambda / SFN                       | Glue / ECS / Batch / EMR              |
| ------------------------- | ---------------------------------- | ------------------------------------- |
| **Your static input**     | from `payload=` / SFN `Input`      | from `glue_arguments=` / container args |
| **Upstream output**       | `event["upstream"][name]["output"]`| `xcom.pull(name)`                     |
| **polyris metadata**      | `event["date"]`, `event["pipeline_name"]` | env: `POLYRIS_RUN_DATE`, `POLYRIS_PIPELINE_NAME`, `POLYRIS_TOKENS_TABLE` |
| **Pipeline variables**    | `event["my_var"]`                  | you route via `glue_arguments=` etc.  |

## Where the data lives

- DynamoDB table `pipeline-tokens`, key `output#{pipeline}#{task}#{date}`.
- **Retention: 120 days** (TTL attribute). Older rows are gone; `pull()` on
  them raises `PullError` — no silent staleness.
- **Same-date re-run overwrites.** A re-run is usually the answer to a broken
  run, so downstream reads the fresh output, not the broken one.
- **Different dates never collide** — a backfill and a live run don't touch
  each other.
- **Size limit ~350 KB** per output. Larger returns truncate; downstream
  `pull()` raises `PullError`. For >350 KB, write to S3 yourself and return
  the path.

## IAM: what `pull()` needs

**Only for `pull()`** — auto-injected `event["upstream"]` needs nothing.

The task's execution role needs `PolyrisTaskReadPolicy` (`dynamodb:GetItem` +
`s3:GetObject`, least-privilege):

```yaml
ManagedPolicyArns:
  - !ImportValue <namespace>-<stage>-polyris-task-read-policy
```

## `xcom.pull()` past a direct dependency

`pull()` looks up by task name + date only — it doesn't consult the DAG. So in
`extract >> transform >> load`, `load` can still reach `extract`:

```python
def load(event):
    from_transform = event["upstream"]["transform"]["output"]   # declared dep
    from_extract   = xcom.pull("extract", event)                # not declared
```

**Tradeoff:** the declaration is what makes polyris *wait* for the upstream.
Skip it and you can start before the upstream produced anything — `pull()`
raises `PullError` rather than returning stale data. Use this only when the
ordering is already guaranteed by other dependencies (here, `extract` runs
before `transform`, which `load` waits for).

## When `pull()` fails

`xcom.pull()` raises `PullError` in exactly four cases:

| Trigger                                                                       | Message                                                                            |
| ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| No context (`event` not passed in Lambda; no `POLYRIS_*` env in ECS/Glue)     | `pull() needs the pipeline name. In a Lambda pass the event …`                     |
| Upstream stored nothing (didn't return, or hasn't run for this date)         | `no output stored for task 'X' (pipeline 'Y', date 'Z') — did it return anything?` |
| Stored `result` isn't valid JSON (rare — bad Lambda return)                  | `stored output for task 'X' is not readable JSON: <parse error>`                   |
| Output truncated (exceeded 350 KB)                                           | `output for task 'X' was truncated and is unavailable …`                           |

The first two cover >90% of the cases. Usually the fix is: copy the exact
task_id from the DAG.

## Determinism

Data flows from the logical run date, not wall-clock time. Both paths
(auto-injected and `pull()`) are deterministic and safe to re-run — as long as
your task's return value doesn't include `now()` or similar.
