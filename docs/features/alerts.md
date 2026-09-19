# Notifications

When a task fails or needs a human decision, polyris surfaces it in the Console. Nothing to configure — the notification bell in the Console header + the Notifications panel are always on.

## What triggers a notification

The Console shows a notification for every event that needs your attention:

| Trigger | Notification kind |
|---------|-------------------|
| Task fails and retries are exhausted (or `retries=0`) | Decision required — the task is in `waiting_decision`, blocking downstream |
| Task's dependency window expires (`wait_for=[asset.within(...)]` timed out) | Decision required |
| Infrastructure error from the wrapper itself (DDB throttle, IAM failure) | Failure warning — surfaced as a `_notify_warn_*` record |
| Backfill completes / partially completes / fails | Backfill status |

Successful runs do not generate notifications — the Console shows their status in the pipeline list.

## Where to see them

- **The bell icon in the Console header** — badge shows the unread count.
- **Click the bell → Notifications panel** opens on the right. Every notification links to the task or run that produced it; clicking one opens the Task Detail modal directly on the relevant tab.
- **The panel groups by pipeline** — so a run with 5 stuck tasks is one visual group, not 5 separate rows.

Notifications auto-refresh every 30s (or 3s when there's an active run in view). No manual reload needed.

## What to do with one

Click the notification → opens Task Detail modal. The task's **Actions** tab has:

| Action | When to use |
|--------|-------------|
| **Restart** | Retry the whole task with a fresh `attempt=1`. Use when the failure was transient (dependency was down, quota reset) |
| **Mark success** | Manually mark the task successful with a synthetic marker. Use when you verified the work is actually done (checked S3, checked the target table) and just want downstream to continue |
| **Skip** | Mark the task `skipped`. Use when the task is not needed for this run (bad input data, partition already handled elsewhere) — downstream `all_success` sees it as a skip cascade |
| **Fail** | Mark the task `failed`. Use when the failure is real and the run should stop cleanly. Downstream `all_success` chains propagate `upstream_failed` |

All four actions record the operator identity + timestamp + free-text reason on the task's synthetic-output marker (see [DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics](DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics) for what downstream tasks see).

## Reading notifications programmatically

For scripts / CI that want to poll for stuck runs without opening the Console:

```bash
curl -s "https://<api-gateway>/api/notifications?limit=20&hours=4" \
  -H "Authorization: Bearer $COGNITO_TOKEN"
```

Query params:
- `limit` — max rows (default 20, cap 100)
- `hours` — how far back to look (default 4)

Response: `{"notifications": [{kind, pipeline_name, task_name, execution_name, created_at, ...}]}`. Poll every 30-60s; each notification is idempotent (same event returns the same row).

## Why the `alerts=` DAG argument was removed

Prior versions of polyris took an `alerts=` argument on `DAG(...)`. It has been removed — passing it now raises `TypeError`. Configuration lives outside the DSL so alerts can be added / changed / removed without touching pipeline code (DSL changes need a redeploy; config doesn't).

## Related

- [DSL.md#trigger-rules](DSL.md#trigger-rules) — how `waiting_decision` and `all_done` interact with a manual action
- [DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics](DATA_PASSING.md#xcommanuallyresolvederror--reading-the-marker-for-diagnostics) — downstream visibility of a manual action
- [ADR #114 — intervention-first failure model](../reference/adr-114-intervention-first-failure-model.md) — why polyris pauses on failure instead of propagating automatically
