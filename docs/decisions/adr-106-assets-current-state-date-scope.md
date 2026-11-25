# ADR #106 — Assets are a current-state surface; per-date history lives only on the matrix

**Context.** The asset console read the global date picker (`useAppStore.date`):
the recent-events feed, the queued-events view, and the queue mutations
(skip/clear/trigger/force) all scoped to the selected day. But three of the four
asset tabs are current by nature — lineage is the live dependency graph, the queue
is what is pending *right now*, and "recent events" reads best as the latest
activity, not "events on 2026-07-05." The only genuinely per-date asset view is the
**matrix** (asset × date grid over a configurable range), which already has its own
range control and its own backfill entry point (clicking a missing/failed cell).
So the global date picker was scoping surfaces that don't need a date, and creating
an app-wide date dependency (pipelines + runs + assets) where assets didn't belong.

**Decision.** Assets are a **current-state** surface. Per-date asset history is
inspected on the **matrix** (its own range control), not through a global date.
1. **Backend (Team).** `GET /api/assets/recent-events` and `GET /api/assets/queued`
   no longer take a `date`; they always use today (the events GSI stays efficient —
   it just always queries today's partition). Default recent-events limit is 30. The
   queue mutations (`skip-in-queue`, `clear-queue`) always act on today's queue.
   `asset-trigger` and force-trigger keep a date (`execution_date` / partition key)
   because that is the *materialization* date and the matrix's backfill needs it.
2. **Frontend (Team).** `useAssetsDataQuery()` drops its `date` argument and the
   `assetsData` query key drops the date; the asset mutations drop their `date`
   argument (trigger/force pass today). The recent-events panel shows the latest 30
   in an internally scrolling container so it doesn't stretch the page.
3. **Frontend (free).** The global date picker is removed from `/assets` (it now
   shows only on `/pipelines` and `/runs`). The pipeline cockpit and the Runs
   workspace own their own date controls (redesign batches), after which the topbar
   picker goes away entirely.

**Consequences.** A user can no longer browse *past-day* asset events or queue from
the console; that history is on the matrix (which also opens backfill). This is
consistent with "assets = current" and removes an app-wide date dependency, at the
cost of the rarely-used past-day event/queue lookup, which the matrix covers.

**Follow-up (v0.93.0).** The same confusion outlived this ADR in the last place still
holding the global date: the **Runs** workspace. Its picker wrote `useAppStore.date` —
the *pipeline page's scope* — so clearing it meant "every date" to the feed and "a day
called `''`" to that page, which then rendered a bare graph with the run it should have
shown sitting right there. Runs now owns `runFilter.date` in its own URL, matching Tasks
and the pipeline page's history drawer. **`useAppStore.date` is the pipeline page's scope
and nothing else** — a workspace view must never read it.

Two ideas, one value, is the whole trap: a feed's empty date means *every date*, a page's
empty date means *nothing at all*. They cannot share storage no matter how convenient it
looks. The backend had the mirror image of it — `params.get('date', <today>)` defaults only
when the param is *absent*, while a cleared picker sends it present and empty — so four
routes matched `date=''` literally. Both halves are pinned by guard tests that walk the
code rather than rely on anyone remembering.
