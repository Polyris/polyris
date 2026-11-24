# How to schedule a pipeline and understand what happens on redeploy

How to schedule a pipeline, pause and change its schedule, redeploy it while runs are in flight, and destroy it. Schedule syntax (cron, rate, preset, asset): [DSL.md#schedule-options](../features/DSL.md#schedule-options).

## What polyris deploys for a schedule

When you set `schedule="cron(...)"` or `schedule="rate(...)"` or `schedule="@daily"` on a DAG, `polyris-deploy` creates an `AWS::Scheduler::Schedule` resource in the pipeline's CFN stack. It has:

- **A trigger** — the cron / rate expression from your DSL string, or the one a preset stands for. Every expression is in **UTC**: polyris sets no time zone, so convert your local time to UTC in the cron.
- **A target** — the pipeline's state machine ARN, invoked via `states:StartExecution`.
- **An execution role** — `{namespace}-{stage}-polyris-{dag_id}-scheduler-role`, trusted only by `scheduler.amazonaws.com`, allowed only to `StartExecution` on this one state machine.
- **A state** — `ENABLED` unless you set `is_paused_upon_creation=True` on the DAG.

`schedule=None` deploys no scheduler resources — the state machine is manual-only (trigger via Console or `states:StartExecution`).

`schedule=[some_asset]` deploys no scheduler either: polyris starts the pipeline when its assets get the new data its schedule asks for. See [ASSETS.md](../features/ASSETS.md).

## When does the first run fire?

EventBridge Scheduler fires on the **next matching wall-clock instant after the schedule is `ENABLED`.**

- `schedule="cron(0 8 * * ? *)"` deployed at 07:30 UTC → first run at 08:00 UTC same day.
- Same schedule deployed at 08:30 UTC → first run at 08:00 UTC *next* day. It doesn't back-fill a missed slot.
- `schedule="rate(1 hour)"` deployed at 12:37 UTC → first run at approximately 13:37 UTC (rate expressions fire "every N units *from now*"; the first tick lands one full period after `ENABLED`).
- `schedule="@daily"` = `cron(0 0 * * ? *)` = midnight UTC; `@hourly` fires on the hour and `@weekly` on Monday at midnight UTC.

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
- Asset-triggered DAGs never have an `AWS::Scheduler::Schedule`: polyris starts them when their assets get new data.

## What happens to in-flight runs when you redeploy

A run keeps the version of the pipeline it started with. `polyris-deploy` records each deploy as a new version; a run in flight goes on with its own, and the next run takes the new one. So a change to a task's retries, retry delay, time limits or trigger rule applies from the next run, also to the tasks of a running run that have not started yet. The console draws each run's graph from its own version.

A changed schedule takes effect at its next tick: change `cron(0 8 * * ? *)` to `cron(0 6 * * ? *)`, and the run that started at 08:00 goes on; the next one starts at 06:00 tomorrow.

### What a redeploy cannot change

- **A task that waits for a decision** (`waiting_decision`). A redeploy doesn't change it: act from the console with Skip, Restart, Fail or Mark Successful.
- **A change to `dag_id`.** That's a *new* pipeline as far as polyris is concerned — the old CFN stack (`{namespace}-{stage}-polyris-{old_dag_id}`) stays put unless you run `polyris-destroy` first. Rename with care.
- **A change to a task's `task_id`.** The next run has the task under its new name only. Runs in flight finish with the old `task_id`.

## Safe redeploy checklist

Before you `polyris-deploy` a running pipeline:

1. **Inspect the local diff.** `git diff dag.py`.
2. **Preview.** `polyris-deploy --dry-run` lists the stack and resources it would deploy, and changes nothing.
3. **Check what's in flight.** Console → pipeline → Runs. A redeploy neither stops those runs nor changes them.
4. **If the change is risky** (a renamed task, a changed trigger rule), pause first: `is_paused_upon_creation=True` → deploy → wait for the runs in flight to end → resume.
5. **`polyris-deploy`.** The next run uses the new version; runs in flight finish on theirs.

## Destroy a pipeline

`polyris-destroy` deletes the pipeline's stack (state machine, schedule, scheduler role, log group) and removes the pipeline from the console. It stops the pipeline's running runs on the way; a task already running may finish first. To stop work at once, Stop the runs in the console first; to let a run finish, wait until it has, then destroy.

Does not delete:

- `pipeline-tokens` rows (the runs' tasks) — kept a year and 90 days from their last write
- `task-events` rows (the tasks' timelines) — kept 90 days
- `asset-events` rows produced by this pipeline's tasks — kept 365 days
- `pipeline-specs` rows (the task specs of each deployed version) — the console draws the graph of an old run from them

## Related

- [DSL.md#schedule-options](../features/DSL.md#schedule-options) — cron / rate / preset / asset syntax
- [CLI.md#polyris-deploy](../reference/CLI.md#polyris-deploy) — every deploy flag
- [ASSETS.md](../features/ASSETS.md) — asset-triggered pipelines (no `AWS::Scheduler::Schedule` deployed)
- [REGISTRATION.md](../features/REGISTRATION.md) — how pipeline registration works
