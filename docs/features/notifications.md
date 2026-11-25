# Notifications in the console

When a task fails, waits for your [decision](../../GLOSSARY.md#decision), or polyris could not carry out part of a run, the console shows a notification under the bell in its header. There is nothing to configure: the bell and its panel are always on. This page lists what makes a notification, how to act on one, and how to read them from a script.

## What triggers a notification

The console shows a notification for every event that needs your attention:

| Trigger | Notification kind |
|---------|-------------------|
| Task fails and retries are exhausted (or `retries=0`) | Decision required — the task is in `waiting_decision`, blocking downstream |
| Task's wait for its dependencies or assets reaches its limit (`orchestration_timeout`, or 7 days for `wait_for`) | Failure — the task is `failed` with the reason |
| polyris could not carry out part of a run: tasks started late because their wake-up was lost, an asset-scheduled run could not start, a [direct step](DSL.md#direct-steps) failed, or a write, a request or a stop failed | Warning, named by its kind |
| A manual action found real output and kept it instead of writing its marker | Warning |

Successful runs make no notification: the console shows their status in the pipeline list.

## Where to see them

- **The bell icon in the console header**: its badge shows the unread count.
- **Click the bell** to open the list. **View** on a notification opens its run on the pipeline page.
- **One notification per run**: a run with five failed tasks shows once, and a task that waits for your decision outranks a failure of the same run.

Notifications refresh every 30 seconds.

Every notification shows by when it happened, whatever the date of its run: a task failure by when the task failed, within the last 48 hours, and a warning by when it was written; warnings expire after 8 days. A task that waits for your decision shows until you decide, with when it fails unless you do (5 hours after its failure); while its run is paused, the pause holds that deadline. Reading or clearing a notification covers that one event: a later failure or decision request of the same run shows as new.

## When coordination itself is unhealthy

The **System** pill in the console header turns amber while task coordination or the console's own checks need attention. For example, the periodic check of waiting tasks (the `sweeper`) has not reported for 15 minutes or could not finish one of its steps. Or asset events are not delivered, waiting tasks are more than 40 minutes behind their checks, or the console cannot reach DynamoDB. Waiting tasks may start late until it clears. The console shows a toast once for each new problem; click the pill for each problem's impact and fix ([TROUBLESHOOTING](../operations/TROUBLESHOOTING.md#the-system-pill-says-needs-attention)). Monitors read `checks.coordination.status` from `GET /api/health`.

## What to do with one

Open the run from the notification, click the task, and open its **Actions** tab:

| Action | When to use |
|--------|-------------|
| **Restart** | Run the task again from its wait: it waits for its dependencies and assets again, and its attempt count goes on. Tick *Also rerun the dependants its failure or stop decided* to rerun them too. Use when the failure was transient (dependency was down, quota reset) |
| **Mark Successful** | Mark the task `success` and record the facts of its outlets: its dependants start if they still wait. Once a rule has decided them after a final failure, Restart each of them, with its dependants, to run them. Use when you verified the work is actually done (checked S3, checked the target table) |
| **Skip** | Mark the task `skipped`. Its dependants run as after a success: `all_success` counts a skip by an operator as done ([Trigger rules](DSL.md#skips-and-who-made-them)). Use when the task is not needed for this run (bad input data, partition already handled elsewhere) |
| **Fail** | Mark the task `failed`. Dependants that need its success end `upstream_failed`, and the rest of the run goes on until every task is final. Use when the failure is real |
| **Stop** | Stop an unfinished task and its work: it ends `stopped`, which its dependants' rules count as a failure. Use when a running task does the wrong thing |

Skip, Fail, Mark Successful and Stop record the operator identity, timestamp and free-text reason on the task's synthetic-output marker, unless the task's real output is already there; Restart writes none (see [DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics](DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics) for what downstream tasks see).

## Reading notifications programmatically

For scripts and CI that poll for stuck runs without opening the console. `API_URL` is the platform stack's output `ConsoleApiUrl`, and [authentication.md](authentication.md#a-token-for-scripts) shows how to get `TOKEN`:

```bash
curl -s "$API_URL/api/notifications?limit=20&hours=4" -H "Authorization: Bearer $TOKEN"
```

Query params:
- `limit` — max rows (default 10, cap 50)
- `hours` — how far back to look (default 24, cap 168)

Response: `{"notifications": [{id, type, pipeline_name, task_name, pipeline_execution, error, date, finished_at, time_ago}]}`. `type` is `failure`, `decision_required` or `warning`; a warning also has a `kind`, and a decision request a `deadline`. Poll every 30-60s; each notification is idempotent (same event returns the same row).

## Related

- [DSL.md#trigger-rules](DSL.md#trigger-rules) — how `waiting_decision` and `all_done` interact with a manual action
- [DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics](DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics) — downstream visibility of a manual action
- [Why a failed task waits for a decision](../reference/adr-114-intervention-first-failure-model.md) instead of failing its dependants at once
