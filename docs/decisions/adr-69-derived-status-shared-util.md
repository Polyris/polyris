---
area: ui
accepted: 2026-05-27
---
# ADR #69 — derivedStatus lifted to shared util + inline Cancel on list

#### Context

ADR #67 introduced `derivedStatus` inside `BackfillDetailPage` to defend
against a stuck `bulk_backfill` SFN — backend reports `status='running'`
forever when the SFN never reaches its Finalize step, even though all
partitions completed. The detail page worked correctly: pill showed
"completed", Cancel button hidden.

The list page (`BackfillsListPage`) did NOT use derivedStatus. Result:
the SAME backfill showed `RUNNING` in the list and `COMPLETED` in the
detail. Mike caught this in v0.78.11 review. Same SFN, two different
truths in the UI.

Additionally: Cancel was reachable only from the detail page. To cancel
a running backfill, users had to navigate from list → row click → wait
for detail load → click Cancel. For a list page where multiple
backfills may need triaging at once, this is annoying.

#### Decision

**1. Extract `computeDerivedBackfillStatus` to `utils/backfillStatus.ts`**.
The same JSONata-mirroring logic now lives in one place. Both
`BackfillDetailPage` and `BackfillsListPage` import it. `isBackfillActive`
and `BACKFILL_TERMINAL_STATUSES` exported alongside.

**2. Status filter switches to client-side**. Was: list query took an
optional API filter (`status=running`, `status=failed`, etc) and the
backend filtered DDB. Problem: filtering by raw `status='completed'`
on the backend would EXCLUDE stuck-running backfills whose derived
status IS completed. So "Completed" tab in the UI would hide them.

New approach: list query always fetches with `null` filter (all
backfills), and the component filters client-side using
`computeDerivedBackfillStatus`. Backfills are infrequent (~hundreds
per pipeline per year), so the over-fetch is cheap.

**3. Inline Cancel button on list rows**. New rightmost column with an
`X` icon button, rendered only when `isBackfillActive(b)` is true.
Behavior identical to detail-page Cancel: `window.confirm`, then
`useCancelBackfillMutation`, toast feedback on success/error. Stops
event propagation so the row click (which navigates to detail) doesn't
also fire. Stuck-running backfills (raw=running, derived=completed)
get NO Cancel button, because `isBackfillActive` returns false for
them — same logic as the pill display.

#### Why not just fix the backend SFN

That's the right ultimate fix, but it requires log-trawling across
stuck SFN executions to find the pattern (timeouts? IAM revocations?
runaway retries?). Until we have that root cause, the client-side
derivation is the minimum-cost safety net. ADR #67 makes the same
argument; this ADR just extends the scope from one page to two.

#### Trade-off: API vs client-side filtering

Pros of client-side:
- Single source of truth for status semantics (`computeDerivedBackfillStatus`)
- Filter results match the displayed pills (no "this looks Completed
  but disappears from Completed tab")
- Stuck-status defense works for filtering, not just rendering

Cons:
- Over-fetch when user uses a filter (downloads all, then filters
  locally). With current volume this is negligible (~hundreds of
  records per pipeline per year). Revisit if list size grows past
  ~5000 records — at that point either paginate or add a derived
  status field to DDB updated by the SFN.

#### Tests

- New `utils/backfillStatus.test.ts` — 10 pure-function tests covering
  terminal-respected, stuck overrides (completed/partial/failed),
  skipped-counts-toward-processed, isBackfillActive truth table.
- `BackfillsListPage.test.tsx` — 5 new tests for stuck-running pill,
  Cancel button visibility (active/terminal/stuck), filter-includes-
  stuck-derived-completed scenario.
- `BackfillDetailPage.test.tsx` — unchanged (now uses shared util but
  behavior unchanged; existing 31 tests catch any regression).

#### Lesson

When applying a defensive heuristic to address a known backend bug,
DON'T apply it in just one place. The bug doesn't care which UI
surface is consuming the data. Either bake it into the data layer
(repo / hook) so every consumer sees the fix automatically, OR
extract it to a shared util that every consumer is expected to call.
This ADR picked the latter because the data layer (React Query
cache + types) didn't have a natural place to inject derived state
without leaking ADR-#67-specific logic into the hooks.
