---
area: ui
accepted: 2026-05-22
---
# ADR #61 — Task-subset selection in backfill UI

#### Context

The Backfill API has supported task_subset (`tasks: ['t1', 't2']`) since
v0.78.0 (ADR #51), but no UI surfaced this control. Users wanting to
re-run only a subset of tasks within a pipeline (e.g. retry a single
processing step without firing scrapers, or re-run the tail of a DAG
after fixing a downstream bug) had to use `curl` directly.

External code review (2026-05-22) flagged this as a feature gap:
"backend ready, UI missing". Combined with ADR #60's runtime
enforcement of `skip_on_backfill`, surfacing task selection lets users
explicitly override the default-skip behavior when they need to.

#### Decision

Add a task-subset multi-select to `BackfillModal`, visible **only for
pipeline targets** and **inside the Options accordion** (collapsed by
default so casual users never see it).

Asset targets do not get this control. Backfilling an asset means
"materialize this asset for the partition"; the producer task is
uniquely determined by the asset's outlets, and any "subset" larger
than that one task is meaningless.

UI contract:

| State | Header label | Sent to API |
|---|---|---|
| Default (no clicks) | "All tasks" | `tasks: null` |
| User unchecks all | "0 selected" | `tasks: []` (still valid; no-op for the pipeline) |
| User picks subset | "N selected" | `tasks: [task_ids]` |
| "None" button | "0 selected" | `tasks: []` |
| "All" button | "All tasks" | `tasks: null` |

Each row shows `task_id`. Rows for tasks with `skip_on_backfill=True`
display a `skip-on-backfill` badge — explicit visual signal that the
default-skip flag applies and ticking the box overrides it (per ADR
#60's user-override semantics).

#### Data source

New lightweight hook `usePipelineTasksList(pipelineName)` fetches
`/api/pipeline-dag?name=X` and projects to
`[{task_id, skip_on_backfill?}, ...]`. Cached 60s in React Query.
Distinct from `usePipelineDetailQuery` (which also pulls execution
status and re-fetches frequently) — for picker purposes only static
metadata is needed.

#### Implementation

State: `selectedTasks: string[] | null` in `BackfillModal`. `null`
means "default (run all)", non-null is the positive subset. Built
into the request just before submit:

```ts
let tasksField: string[] | null = seed.tasks ?? null;
if (target.type === 'pipeline' && selectedTasks && selectedTasks.length > 0) {
    tasksField = selectedTasks;
}
```

`seed.tasks` (from task-detail entry point) remains the pre-fill;
user explicit selection in the modal overrides it.

#### Tests

Five vitest tests in `BackfillModal.test.tsx::task subset selection`:

- Selector renders for pipeline targets (header + 3 rows).
- `skip_on_backfill` rows display the flag badge.
- Selector is **absent** for asset targets.
- Default state shows "All tasks".
- Clicking a checkbox transitions from default to "N selected".

#### Backwards compatibility

Pure additive UI. No API change. `tasks: null` (the default) preserves
v0.78.0 behavior — full pipeline backfill subject to `skip_on_backfill`
enforcement (ADR #60).

#### Alternatives considered

1. **Show selector for all target types** — semantically wrong for
   assets (one task = one outlet). Confusing UX.
2. **Expose selector outside Options accordion** — overwhelms casual
   users. The whole point of "ideal for any user" (Mike, 2026-05-22)
   is that the simple case stays simple; subset selection is power-
   user territory.
3. **Separate "Advanced backfill" modal** — splits cognitive load
   across two entry points. Rejected: one modal that grows by
   expansion is simpler than two modals.
