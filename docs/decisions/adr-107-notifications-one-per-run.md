# ADR #107 — Notifications: one per run, two attention states (failure vs decision-required)

**Context.** The header notification feed scanned task-instance records for
`status == 'failed'` and deduped to one entry per run. Two gaps surfaced in live use:
(1) a task that failed and then **paused awaiting a manual decision** carries status
`waiting_decision`, not `failed`, so it never produced a notification — even though it
is the case that most needs attention (the run is blocked on a human). (2) There was
no distinction between "a task failed" and "the run needs a decision"; conceptually
"task failed" and "pipeline failed" are the same event (a run fails *because* a task
fails), so alerting on both would double-notify. Industry practice (DAG-level
callbacks, Dagster/Prefect run-level alerts) is to alert at the **run level** with the task as context, and
to treat human-gated pauses as their own signal.

**Decision.** Notifications are computed **one per run**, classified into two states:
1. **`failure`** (red) — a task in the run has `status == 'failed'`. Time-windowed on
   `finished_at` (must fall within the query window). Names the failed task as context.
2. **`decision_required`** (orange, `--decision` / `text-orange-500`) — a task in the
   run has `status == 'waiting_decision'`. A paused task has no `finished_at`, so it is
   anchored on `running_at`/`started_at` and always surfaces (it is actively blocking).
3. **Priority:** when a run has both, `decision_required` wins (it needs human action).
   The `notifications.py` filter is `Attr('status').is_in(['failed','waiting_decision'])`
   and per-run state is accumulated (not first-wins) so the decision state can upgrade a
   failure entry for the same run.

**Consequences.** A blocked-on-decision run now alerts (previously silent), and a run
never yields more than one notification regardless of how many tasks failed. We do
*not* add a separate "task failed while the run is still running" notification (kept out
to avoid noise — in-progress task failures are visible on the DAG). The classification
is derived purely from task records; no run-level status record is introduced.
