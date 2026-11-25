# How to configure task retries

You have a task that fails now and then, and you want polyris to retry it before it waits for a person. This guide covers what the five retry parameters do, which combination fits which failure, and what happens after the last retry.

Parameter reference (types and defaults): [DSL.md#common-parameters](../features/DSL.md#common-parameters-apply-to-every-task-type).

## What polyris retries mean

A task that fails retries in place: polyris waits by your policy and runs the work again.

Two kinds of retry are easy to confuse; polyris handles both, and only the first is yours to set:

| Layer | What it retries | Configurable? |
|-------|-----------------|----------------|
| **Your retries** (this doc) | The whole task, after its work failed | Yes — `retries` / `retry_delay` / `retry_exponential_backoff` / `retry_jitter` / `max_retry_delay` |
| **Service retries** | Throttling and service-limit errors when starting the work; polyris retries them a few times on its own | No |

When AWS reports that the work itself failed (the Lambda handler raised, the Glue job exited non-zero, the Athena query ended `FAILED`), your retries apply. Only the service errors of the second row are retried before your `retries` count.

## Retries pause the task, they don't erase the failure

After the last retry fails, polyris does **not** fail the task's dependants at once. The task waits for your decision in the console (`waiting_decision`); if nobody decides within 5 hours, the task fails:

- **Restart** — try the whole retry sequence again: the retries count from the first again, and the task's ↻ goes up by one
- **Skip** — mark the task `skipped`; downstream rules honor it
- **Mark Successful** — mark the task `success`; downstream continues
- **Fail** — mark the task `failed`; downstream `all_success` chains propagate `upstream_failed`

This matters when you pick a retry count: **retries buy the task a chance to self-heal before waking someone up.** Higher retries reduce night-time pages but delay the human decision when the failure is genuinely stuck.

## The five parameters, briefly

| Parameter | Default | Effect |
|-----------|---------|--------|
| `retries` | `0` | Attempts *after* the first failure. `retries=3` = up to 4 total attempts. `0` = pause on first failure. Maximum value: `9` |
| `retry_delay` | `timedelta(minutes=5)` | Base wait between attempts. With backoff off, wait is exactly this every time |
| `retry_exponential_backoff` | `False` | If `True`, wait doubles each attempt: `min(retry_delay * 2^attempt, max_retry_delay)` |
| `max_retry_delay` | `None` (cap of 3600s applies) | Ceiling for the exponential curve. Only relevant with backoff on |
| `retry_jitter` | `False` | If `True`, wait is randomised into `[base/2, base)` — equal jitter, avoids thundering herd |

`attempt` in the backoff formula starts at 0 for the first retry wait, so with `retry_delay=timedelta(seconds=10)` and `retry_exponential_backoff=True` the waits are 10s, 20s, 40s, 80s, …, capped by `max_retry_delay`.

## Pick a pattern

### Task calls a rate-limited external API

```python
@task.lambda_function(
    function_name="fetch-shopify-orders",
    retries=5,
    retry_delay=timedelta(seconds=30),
    retry_exponential_backoff=True,
    retry_jitter=True,
    max_retry_delay=timedelta(minutes=10),
)
def fetch_orders():
    pass
```

Waits: ~15-30s, ~30-60s, ~60-120s, ~120-240s, ~240-480s (jitter halves each range's floor). Jitter matters when several tasks retry against the same rate-limited endpoint — without it they all resume at the same second and re-trigger the limit. Total wait budget: ~15 minutes worst case.

### Task depends on another service that's briefly down

```python
@task.glue_job(
    job_name="daily-etl",
    retries=3,
    retry_delay=timedelta(minutes=2),
    retry_exponential_backoff=True,
)
def daily_etl():
    pass
```

Waits: 2m, 4m, 8m. Simple exponential without jitter — this is one task, no herd concern. The fourth failure waits for human review (usually the dependency's real outage).

### Task is expensive; retrying blindly costs money

```python
@task.batch_job(
    job_definition="ml-training",
    job_queue="high-mem",
    retries=1,
    retry_delay=timedelta(minutes=1),
)
def train_model():
    pass
```

One retry covers the "SPOT instance evicted mid-run" case. Beyond that, wake a human — you don't want to spend 40 minutes of GPU time retrying a code bug.

### Task must not silently succeed after transient corruption

```python
@task.athena_query(
    query_string="INSERT INTO ...",
    database="analytics",
    retries=0,   # explicit — this is a data-integrity task
)
def daily_insert():
    pass
```

`retries=0` (the default). One shot, pause on failure. Human decides whether the partial insert is recoverable or the whole partition needs a manual delete + rerun. Automated retries on `INSERT` can double-write rows if the failure happened after the write but before the `success` return.

## DAG-level default that per-task overrides

Set sane defaults once, then override for outliers:

```python
with DAG(
    "orders-etl",
    schedule="@daily",
    default_args={
        "retries": 3,
        "retry_delay": timedelta(minutes=5),
        "retry_exponential_backoff": True,
        "retry_jitter": True,
    },
) as dag:

    @task.glue_job(job_name="normal-etl")   # inherits: 3 retries, 5m base, backoff+jitter
    def normal_etl():
        pass

    @task.athena_query(query_string="INSERT ...", database="analytics", retries=0)
    def daily_insert():   # overrides: no retries, data-integrity task
        pass
```

`default_args` applies to every task in the DAG unless the task's own decorator overrides the field. Overrides are per-parameter — setting `retries=0` on `daily_insert` doesn't reset the other retry parameters (they stay from `default_args`), but they're inert when `retries=0`.

## What retries don't help with

- **A code bug that always throws.** Ten attempts of the same broken handler = ten identical failures + a delayed page. Set `retries=0` for code you know is deterministic, or ship a fix instead of adding retries.
- **A schema mismatch on a Glue Data Catalog table.** Athena won't recover from `Column 'foo' cannot be resolved` no matter how many times you re-issue the query. Fix the table.
- **A `SIGKILL` from OOM.** ECS/Batch task killed by the kernel gets a fresh container on retry — same allocation, same OOM. Bump memory instead.
- **Any failure whose cause is upstream.** If `extract` fails, `transform` does not run at all; give `extract` the retries.

## Debugging retry behavior

- Console → Task Detail → **History** tab shows every dispatch: one **Started** event for the first dispatch, then one **Auto-retry** event per automatic retry. If you configured `retries=3` and see only two **Auto-retry** events, either the task succeeded on that retry, or a human acted on the pause before the third retry.
- Console → Task Detail → **Details** tab → **Attempt** stat tile shows two chips when retries have occurred:
  - ↻ N — number of manual Restarts (bumps only when an operator clicks Restart)
  - ⭮ N/M — the current automatic retry / the retries configured
  - Neither chip appears when the task was never restarted and has had no auto-retry.
- Console → **Tasks** view → **Status** column shows the same chips inline for at-a-glance visibility across all tasks.
- The task's node in the pipeline graph shows a countdown while `run_task` waits between retries — the same countdown as for `wait_before` delays.
- The task's `run_task` execution in the Step Functions console (the task's **AWS Console** link) shows the retry loop's states: `Prepare_Retry_Wait`, `Wait_Before_Retry`, `Save_Retry_Attempt`.

## Related

- [DSL.md#common-parameters](../features/DSL.md#common-parameters-apply-to-every-task-type) — parameter reference (types, defaults)
- [LOCAL_TESTING.md](../tools/LOCAL_TESTING.md) — mock runs check structure and trigger rules; retries are not simulated
