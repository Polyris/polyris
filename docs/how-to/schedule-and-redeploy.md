# How to schedule a pipeline and understand what happens on redeploy

Two lifecycle questions this doc answers:

1. When does a scheduled pipeline first fire? What does polyris deploy when I set `schedule="..."`?
2. What happens to a run that's already in-flight when I `polyris-deploy` a new version?

For the raw schedule syntax (cron / rate / preset / asset), see [DSL.md#schedule-options](../features/DSL.md#schedule-options). This doc is the lifecycle companion — what happens *around* the schedule at deploy time.

## What polyris deploys for a schedule

When you set `schedule="cron(...)"` or `schedule="rate(...)"` or `schedule="@daily"` on a DAG, `polyris-deploy` creates an `AWS::Scheduler::Schedule` resource in the pipeline's CFN stack. It has:

- **A trigger** — the cron / rate expression, converted verbatim from your DSL string. All expressions are interpreted in **UTC**. Polyris does not currently support `ScheduleExpressionTimezone`; convert your intended local time to UTC in the cron.
- **A target** — the pipeline's state machine ARN, invoked via `states:StartExecution`.
- **An execution role** — `{pipeline}-scheduler-role`, trusted only by `scheduler.amazonaws.com`, allowed only to `StartExecution` on this one state machine. Scoped tight; nothing else.
- **A state** — `ENABLED` unless you set `is_paused_upon_creation=True` on the DAG.

`schedule=None` deploys no scheduler resources — the state machine is manual-only (trigger via Console or `states:StartExecution`).

`schedule=[some_asset]` deploys no scheduler either — the platform's asset-fanout function starts the pipeline when the facts of its assets satisfy its schedule, not EventBridge. See [ASSETS.md](../features/ASSETS.md).

## When does the first run fire?

EventBridge Scheduler fires on the **next matching wall-clock instant after the schedule is `ENABLED`.**

- `schedule="cron(0 8 * * ? *)"` deployed at 07:30 UTC → first run at 08:00 UTC same day.
- Same schedule deployed at 08:30 UTC → first run at 08:00 UTC *next* day. It doesn't back-fill a missed slot.
- `schedule="rate(1 hour)"` deployed at 12:37 UTC → first run at approximately 13:37 UTC (rate expressions fire "every N units *from now*"; the first tick lands one full period after `ENABLED`).
- `schedule="@daily"` = `rate(1 day)`: the first run comes a day after the schedule is created, then every 24 hours. For midnight UTC, write `schedule="cron(0 0 * * ? *)"`.

If you need history for dates before deploy, start the pipeline manually for each date you want to fill in. Press **Run** on the pipeline's page in the console and set `current_date` in its input, or call Step Functions `StartExecution` with `{"current_date": "YYYY-MM-DD"}`.

## Pause and resume a schedule

Two ways to pause, in order of what to reach for. Either way, runs in flight keep going and no *new* run starts. The polyris console's **Pause** pauses a run, not its schedule.

**1. Change the DAG and redeploy.** Add `is_paused_upon_creation=True` to `DAG(...)` and `polyris-deploy`. The schedule deploys `DISABLED`. `polyris-deploy` re-applies the DAG's declared state on every deploy, so a pause made in the AWS console reverts the next time you deploy.

```python
with DAG(
    "orders-etl",
    schedule="@daily",
    is_paused_upon_creation=True,   # deploy with the schedule off
) as dag:
    ...
```

**2. In the AWS console, for a one-off pause.** EventBridge → Scheduler → Schedules → `{namespace}-{stage}-polyris-{dag_id}-schedule` → **Disable**. The next `polyris-deploy` enables it again unless the DAG has `is_paused_upon_creation=True`: the DSL is the source of truth. The AWS CLI's `aws scheduler update-schedule` replaces the whole schedule, so it needs every field of it, not only `--state`.

Resume: **Enable** in the same place, or remove `is_paused_upon_creation=True` and redeploy.

## Change the schedule

Edit `schedule="..."` in `dag.py` and `polyris-deploy`. The `AWS::Scheduler::Schedule` resource updates in place — no downtime, no orphan schedules. In-flight runs continue on their existing execution; the next trigger uses the new expression.

Switching between schedule *types* (cron → asset, or asset → cron) recreates the resource:

- Removing a cron schedule (setting `schedule=None` or `schedule=[some_asset]`) deletes the `AWS::Scheduler::Schedule` in the next deploy.
- Adding a cron schedule to a previously manual-or-asset DAG creates it.
- Asset-triggered DAGs never have an `AWS::Scheduler::Schedule` — asset-fanout starts them from their `asset-subscriptions` rows.

## What happens to in-flight runs when you redeploy

You're at a pipeline that has one or more runs currently executing. You `polyris-deploy` a new version of `dag.py`. Here's what changes and what doesn't.

### The state machine definition updates. In-flight executions keep the OLD definition.

`polyris-deploy` calls CloudFormation, which calls `states:UpdateStateMachine`. AWS documents this behavior explicitly:

> Existing executions started before the update do not use the new definition. They continue with the definition they were started with.

The state machine holds the run's start and its direct steps; the tasks come from the spec version the run started with (below). Either way, a change takes effect on the **next** run. If you added `retries=3` to a task, the task that's currently on attempt 1 does not gain retries.

### The schedule expression updates. It affects the next tick.

If you changed `schedule="cron(0 8 * * ? *)"` to `schedule="cron(0 6 * * ? *)"`, the current in-flight run (triggered at 08:00) is unaffected. The scheduler fires next at 06:00 tomorrow, not 08:00.

### A run keeps the version it started with.

`polyris-deploy` writes the pipeline's task specs as a new version in `pipeline-specs` before the CFN deploy, and each run reads the version it started with. The Console shows the new graph for the runs that start after the deploy; a run in flight keeps its own graph.

### Task settings change from the next run.

A task's retries, retry delay, time limits and trigger rule come from its run's version. A redeploy that changes them does not touch a run in flight, the tasks that have not started yet in it included: they take the new values on the *next* run.

### What redeploy CANNOT recover

- **A task that waits for a decision** (`waiting_decision`). Redeploying doesn't unstick it. Act from the console: Skip, Restart, Fail or Mark Successful.
- **A change to `dag_id`.** That's a *new* pipeline as far as polyris is concerned — the old CFN stack (`{namespace}-{stage}-polyris-{old_dag_id}`) stays put unless you run `polyris-destroy` first. Rename with care.
- **A change to a task's `task_id`.** The task disappears from the next run: a run takes its tasks from the version it started with. In-flight runs finish with the old `task_id`; their `pipeline-tokens` rows stay a year and 90 days, and their `task-events` rows 90 days.

## Safe redeploy checklist

Before you `polyris-deploy` a running pipeline:

1. **Inspect the local diff.** `git diff dag.py` — you know exactly what changed.
2. **Preview the CFN change.** `polyris-deploy --dry-run` — shows the resource-level diff CloudFormation will apply.
3. **Check what's in-flight.** Console → pipeline → Runs — do you have runs mid-execution you don't want to interfere with? (Redeploy doesn't kill them; it also doesn't help them.)
4. **If the change is risky** (renamed a task, changed a trigger rule, tightened retries), consider pausing first: `is_paused_upon_creation=True` → deploy → wait for in-flight to drain → resume.
5. **`polyris-deploy`.** New state machine version live; next scheduled trigger uses it; in-flight runs finish on the old version.

## Destroy a pipeline

`polyris-destroy` deletes the pipeline's CFN stack (state machine + schedule + scheduler role + log group). While CloudFormation deletes it, `polyris-destroy` stops the pipeline's running runs and prints each one. CloudFormation deletes the state machine only once its executions have ended, and a run keeps its execution open until every task of it is final, a paused run forever. The engine stops the tasks of each stopped run at its next check of the run; until then the run goes on, and a task that finishes records its success and starts its dependants. To stop new work at once, Stop the run in the console first. To let a run finish, wait until it has, then destroy. Once the stack is gone, `polyris-destroy` removes the pipeline from the console: its row in the pipeline registry and its subscriptions to the assets of its schedule.

Does not delete:

- `pipeline-tokens` rows (the runs' tasks) — kept a year and 90 days from their last write
- `task-events` rows (the tasks' timelines) — kept 90 days
- `asset-events` rows produced by this pipeline's tasks — kept 365 days
- `pipeline-specs` rows (the task specs of each deployed version) — the console draws the graph of an old run from them

## Related

- [DSL.md#schedule-options](../features/DSL.md#schedule-options) — cron / rate / preset / asset syntax
- [DEPLOY.md](../deployment/DEPLOY.md) — `polyris-deploy` CLI reference
- [CLI.md#polyris-deploy](../reference/CLI.md#polyris-deploy) — every deploy flag
- [ASSETS.md](../features/ASSETS.md) — asset-triggered pipelines (no `AWS::Scheduler::Schedule` deployed)
- [REGISTRATION.md](../features/REGISTRATION.md) — how pipeline registration works
