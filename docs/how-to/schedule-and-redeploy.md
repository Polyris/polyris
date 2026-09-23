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

`schedule=[some_asset]` deploys no scheduler either — the pipeline is triggered by asset publish events via the shared polyris `notify_asset_consumers` state machine, not EventBridge. See [ASSETS.md](../features/ASSETS.md).

## When does the first run fire?

EventBridge Scheduler fires on the **next matching wall-clock instant after the schedule is `ENABLED`.**

- `schedule="cron(0 8 * * ? *)"` deployed at 07:30 UTC → first run at 08:00 UTC same day.
- Same schedule deployed at 08:30 UTC → first run at 08:00 UTC *next* day. It doesn't back-fill a missed slot.
- `schedule="rate(1 hour)"` deployed at 12:37 UTC → first run at approximately 13:37 UTC (rate expressions fire "every N units *from now*"; the first tick lands one full period after `ENABLED`).
- `schedule="@daily"` = `cron(0 0 * * ? *)` = midnight UTC.

If you need history for dates before deploy, start the pipeline manually for each date you want to fill in — either from the Console UI's Pipelines page (Actions → Run) or by calling Step Functions `StartExecution` directly with `{"date": "YYYY-MM-DD"}`.

## Pause and resume a schedule

Three ways to pause, in order of what to reach for:

**1. Console UI — the day-to-day path.** Pipelines page → open pipeline → **Pause**. Sets the schedule `DISABLED` in EventBridge Scheduler and marks the pipeline paused in `pipeline-registry` DDB. In-flight runs keep going; no *new* runs are triggered.

**2. Change the DAG and redeploy.** Add `is_paused_upon_creation=True` to `DAG(...)` and `polyris-deploy`. The schedule deploys `DISABLED`. Also fixes a schedule you manually paused via the AWS console: `polyris-deploy` re-applies the DAG's declared state on every deploy, so a manual console override reverts the next time you deploy.

```python
with DAG(
    "orders-etl",
    schedule="@daily",
    is_paused_upon_creation=True,   # deploy paused; enable later via Console
) as dag:
    ...
```

**3. Directly via AWS CLI.** `aws scheduler update-schedule --name {pipeline}-schedule --state DISABLED`. Fast for a one-off manual pause, but `polyris-deploy` will re-`ENABLED` it if the DAG doesn't have `is_paused_upon_creation=True` — the DSL is the source of truth.

Resume: Console → **Resume**, or `--state ENABLED` in the CLI, or remove `is_paused_upon_creation=True` and redeploy.

## Change the schedule

Edit `schedule="..."` in `dag.py` and `polyris-deploy`. The `AWS::Scheduler::Schedule` resource updates in place — no downtime, no orphan schedules. In-flight runs continue on their existing execution; the next trigger uses the new expression.

Switching between schedule *types* (cron → asset, or asset → cron) recreates the resource:

- Removing a cron schedule (setting `schedule=None` or `schedule=[some_asset]`) deletes the `AWS::Scheduler::Schedule` in the next deploy.
- Adding a cron schedule to a previously manual-or-asset DAG creates it.
- Asset-triggered DAGs never have an `AWS::Scheduler::Schedule` — they get their triggers from `asset-subscriptions` DDB writes handled by the polyris `notify_asset_consumers` state machine.

## What happens to in-flight runs when you redeploy

You're at a pipeline that has one or more runs currently executing. You `polyris-deploy` a new version of `dag.py`. Here's what changes and what doesn't.

### The state machine definition updates. In-flight executions keep the OLD definition.

`polyris-deploy` calls CloudFormation, which calls `states:UpdateStateMachine`. AWS documents this behavior explicitly:

> Existing executions started before the update do not use the new definition. They continue with the definition they were started with.

Practical consequence: a change to task retry policy, trigger rule, or DAG topology takes effect on the **next** run. In-flight runs finish with the old policy. If you added `retries=3` to a task, the task that's currently on attempt 1 does not gain retries.

### The schedule expression updates. It affects the next tick.

If you changed `schedule="cron(0 8 * * ? *)"` to `schedule="cron(0 6 * * ? *)"`, the current in-flight run (triggered at 08:00) is unaffected. The scheduler fires next at 06:00 tomorrow, not 08:00.

### Registration re-fires. DAG snapshot in the registry updates.

The pipeline's `PipelineRegistration` Custom Resource re-runs on any template change. Writes the new DAG snapshot to `pipeline-registry` DDB. The Console UI reflects the new topology within seconds. In-flight runs continue to render against their *own* snapshot (captured at run start), so their DAG view in the Console keeps showing the old shape until they finish.

### Task-config changes: read at task START, not at wrapper start.

The dependency wrapper for a task that's currently in `waiting` state reads its `task_config` (retries, retry_delay, timeouts, trigger_rule) from the DAG snapshot captured when the pipeline execution started. So even for tasks that haven't fired yet in the current run, a redeploy that changes those parameters does not affect them. They pick up the new values on the *next* pipeline execution.

### What redeploy CANNOT recover

- **A run that's currently paused for a manual decision** (task in `waiting_decision`). Redeploying doesn't unstick it. Act via Console (Skip / Restart / Fail / Mark Success) or `aws stepfunctions send-task-success/failure` with the stored task token.
- **A change to `dag_id`.** That's a *new* pipeline as far as polyris is concerned — the old CFN stack (`{namespace}-{stage}-polyris-{old_dag_id}`) stays put unless you `polyris-deploy --destroy` it first. Rename with care.
- **A change to a task's `task_id`.** The task disappears from the DAG in the next run's snapshot. In-flight runs finish with the old task_id; the task-events / pipeline-tokens rows for the old id stay in DDB (subject to TTL, ~120 days).

## Safe redeploy checklist

Before you `polyris-deploy` a running pipeline:

1. **Inspect the local diff.** `git diff dag.py` — you know exactly what changed.
2. **Preview the CFN change.** `polyris-deploy --dry-run` — shows the resource-level diff CloudFormation will apply.
3. **Check what's in-flight.** Console → pipeline → Runs — do you have runs mid-execution you don't want to interfere with? (Redeploy doesn't kill them; it also doesn't help them.)
4. **If the change is risky** (renamed a task, changed a trigger rule, tightened retries), consider pausing first: `is_paused_upon_creation=True` → deploy → wait for in-flight to drain → resume.
5. **`polyris-deploy`.** New state machine version live; next scheduled trigger uses it; in-flight runs finish on the old version.

## Destroy a pipeline

`polyris-deploy --destroy` deletes:

- The pipeline's CFN stack (state machine + schedule + scheduler role + log group)
- The `pipeline-registry` DDB row (Console UI drops the pipeline from the sidebar)
- The `asset-subscriptions` DDB rows this pipeline held (stops phantom triggers)

Does not delete:

- `pipeline-tokens` rows (task execution history) — subject to TTL, ~120 days
- `task-events` rows — subject to TTL, ~120 days
- `asset-events` rows produced by this pipeline's tasks — kept indefinitely by default

In-flight executions abort when the state machine is deleted (CFN calls `DeleteStateMachine`, which stops all running executions). If you need them to finish, stop the schedule first (`is_paused_upon_creation=True` + deploy), wait for in-flight to drain via the Console, then `--destroy`.

## Related

- [DSL.md#schedule-options](../features/DSL.md#schedule-options) — cron / rate / preset / asset syntax
- [DEPLOY.md](../deployment/DEPLOY.md) — `polyris-deploy` CLI reference
- [CLI.md#polyris-deploy](../reference/CLI.md#polyris-deploy) — every deploy flag
- [ASSETS.md](../features/ASSETS.md) — asset-triggered pipelines (no `AWS::Scheduler::Schedule` deployed)
- [REGISTRATION.md](../features/REGISTRATION.md) — how the registration Custom Resource works
