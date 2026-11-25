---
area: ui
accepted: 2026-05-27
---
# ADR #62 — Backfill cost estimate removed pending Pro tier

#### Context

ADR #53 (v0.78.0) introduced `estimated_sfn_cost_usd` in the `/api/backfill`
response, surfaced in `BackfillsListPage` (column "Cost"),
`BackfillDetailPage` ("Estimated cost" row), and `BackfillModal` (preview
line). The methodology — modeling bulk-backfill SFN transitions + child
pipeline transitions — was technically sound and positioned as a
serverless differentiator vs competing orchestrators.

Live UI revealed a different problem. The label "Cost" reads to users as
"what this run cost" (actuals), while the value is "what we expect this
to cost at submit time" (estimate). The two diverge in two directions:

- **Optimistic divergence**: estimate assumes ideal Lambda execution
  duration. Real runs hit cold starts, retries, and per-task variance.
  Actual usage can be 1.5–3× estimate on small backfills (where init
  overhead dominates).
- **Pessimistic divergence**: skip-completed partitions short-circuit at
  submit time, but our estimate counted them before skip. UI showed
  $0.05 when actual SFN bill was $0.01.

Either way, the displayed number was at best directionally correct, never
exact. Users acted on it (or worse — quoted it to stakeholders) as if
it were a bill.

#### Decision

Remove `estimated_sfn_cost_usd` from API responses, DDB writes, UI
displays, and the `PartitionRange.cost_estimate()` SDK helper. Replace
nothing in Community tier.

Cost reporting becomes a **Pro-tier feature** spec'd as one coherent
workflow: estimate-at-submit + actuals-reconciled-post-run +
budget-enforcement. None of these are useful without the others, so we
ship none of them until we ship all of them.

#### What changed

| Layer | Before | After |
|---|---|---|
| `POST /api/backfill` response | `estimated_sfn_cost_usd: 0.0047` | Field removed |
| `POST /api/backfill?preview=true` response | Same | Field removed |
| DDB `pipeline-tokens` (backfill record) | Wrote `estimated_sfn_cost_usd` | No write |
| `polyris.partitions.PartitionRange.cost_estimate()` | Public method | Removed |
| `dal/ddb_schema.py BackfillCols.ESTIMATED_SFN_COST_USD` | Constant | Removed |
| `BackfillsListPage` | "Cost" column | Column removed |
| `BackfillDetailPage` | "Estimated cost" meta row | Row removed |
| `BackfillModal` | "Estimated SFN cost: $0.0047" preview line | Line removed |
| UI `BackfillStartResponse` / `BackfillPreviewResponse` / `BackfillSummary` types | `estimated_sfn_cost_usd` field | Field removed |

ADR #53 is **partially superseded** — methodology stays in git history
for the future Pro implementation; current Community surface is removed.

Existing DDB records with `estimated_sfn_cost_usd` are untouched; the
read path simply no longer projects the field. Cleanup is not required
(field occupies negligible space).

#### Backwards compatibility

Mild break for any external consumer that read
`estimated_sfn_cost_usd` from the API. We don't know of any (UI is the
sole consumer in this codebase), and v0.78.0 was pre-1.0 with no
external-contract guarantees. CHANGELOG entry under "Removed" signals
the change explicitly.

CLI (`tests/sdk/test_cli.py`) had a stub response containing the field;
updated to remove it. Any downstream tooling that parsed the field
must catch `KeyError` / `null`.

#### Tests removed

`tests/sdk/test_partitions.py::TestCostEstimate` (5 tests) — deleted
entirely per CLAUDE.md #11 (no skipped tests). The class no longer
exists.

`tests/e2e/test_backfill.py` — two `'estimated_sfn_cost_usd' in body`
assertions removed.

`sam/lambdas/console_api/tests/routes/test_backfill.py` — no changes
needed (existing tests didn't assert the cost field).

#### Alternatives considered

1. **Rename "Cost" → "Est. Cost" + tooltip** — band-aid; still misleads
   users into treating estimate as actual.
2. **Show estimate only in `BackfillModal` (pre-submit decision support)**
   — drops the list/detail views but keeps the modal. Inconsistent across
   surfaces and still half-deliverable.
3. **Keep estimate, add actuals via post-run reconciliation** — that's
   the Pro spec; doing it now would mean shipping a partial implementation
   in Community.

Option (3) is the spec; doing it as Community defeats the tier
differentiation. Option chosen because (a) honest about the gap,
(b) preserves Pro feature integrity, (c) shrinks Community surface area.
