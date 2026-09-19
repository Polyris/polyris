# How to configure task retries

You have a task that fails intermittently and you want polyris to retry it before pausing for a human. This guide covers what the five retry parameters actually do, which combinations to use when, and what happens after retries are exhausted.

For the parameter reference (types + defaults) see [DSL.md#common-parameters](../features/DSL.md#common-parameters-apply-to-every-task-type). This doc is the *task-oriented* companion — pick the pattern that matches your failure mode.

## What polyris retries mean

A task that fails *fully* (task code returned an error, or the AWS service reported failure after all its own internal retries) enters polyris's retry loop. Each retry attempt reuses the same task record — the `attempt` counter bumps, the task re-executes, and the wrapper waits according to your policy between attempts.

Two layers of retry that are easy to confuse — polyris takes care of both, and only the first one is your knob:

| Layer | What it retries | Configurable? |
|-------|-----------------|----------------|
| **Polyris user retries** (this doc) | The whole task, after it *fully* failed | Yes — `retries` / `retry_delay` / `retry_exponential_backoff` / `retry_jitter` / `max_retry_delay` |
| **AWS SDK service retries** | Transient AWS API errors during a single task execution (throttling, `Lambda.ServiceException`, DynamoDB throttles) | No — hard-coded (`MaxAttempts=3`, `IntervalSeconds=2`, `BackoffRate=2`), applied by the wrapper to every AWS call |

When AWS says "the task itself failed" (Lambda returned a handler exception, Glue job exited non-zero, Athena query got `FAILED`) — that's a full task failure and polyris user retries kick in. Throttling and transient AWS errors are absorbed by the SDK layer before your `retries` count is even consulted.

## Retries pause the task, they don't erase the failure

After the last retry fails, polyris does **not** propagate failure automatically (ADR #114). The task lands in `waiting_decision` and blocks pending a human choice in the Console:

- **Restart** — try the whole retry sequence again (fresh `attempt=1`)
- **Skip** — mark the task `skipped`; downstream rules honor it
- **Mark success** — mark the task `success` with a synthetic marker; downstream continues
- **Fail** — mark the task `failed`; downstream `all_success` chains propagate `upstream_failed`

This matters when you pick a retry count: **retries buy the task a chance to self-heal before waking someone up.** Higher retries reduce night-time pages but delay the human decision when the failure is genuinely stuck.

## The five parameters, briefly

| Parameter | Default | Effect |
|-----------|---------|--------|
| `retries` | `0` | Attempts *after* the first failure. `retries=3` = up to 4 total attempts. `0` = pause on first failure |
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

Waits: ~15-30s, ~30-60s, ~60-120s, ~120-240s, ~300-600s (jitter halves each range's floor). Jitter matters when several tasks retry against the same rate-limited endpoint — without it they all resume at the same second and re-trigger the limit. Total wait budget: ~15 minutes worst case.

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

Waits: 2m, 4m, 8m. Simple exponential without jitter — this is one task, no herd concern. Third failure pauses for human review (usually the dependency's real outage).

### Task is expensive; retrying blindly costs money

```python
@task.batch_job(
    job_definition="ml-training:1",
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
- **Any failure whose cause is upstream.** If `extract` fails, retrying `transform` (which reads `extract`'s output) succeeds only if `extract` was also configured to retry. Retries don't cross task boundaries.

## Debugging retry behavior

- Console → Task Detail → **History** tab shows every attempt: start time, duration, status, error message. If you configured `retries=3` and see only two attempts, either the task succeeded, or a human acted on the pause before the third retry.
- Console → Task Detail → **Details** tab → `attempt` field shows the current attempt number (1-indexed).
- CloudWatch log group `/aws/vendedlogs/states/polyris-run-task-helper` — filter by execution ARN to see the retry loop's state transitions (`Wait_Between_Retries` state entries).

## Related

- [DSL.md#common-parameters](../features/DSL.md#common-parameters-apply-to-every-task-type) — parameter reference (types, defaults)
- ADR #107 — the wrapper retry loop design (why in-place, why the wait formula)
- ADR #114 — intervention-first failure model (why retries exhaust into a human decision, not automatic propagation)
- [LOCAL_TESTING.md](../tools/LOCAL_TESTING.md) — `run(dag, mock=True)` for testing retry logic locally
