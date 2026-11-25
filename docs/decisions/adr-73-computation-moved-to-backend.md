---
area: console-api
accepted: 2026-05-27
---
# ADR #73 — Computation moved to backend — derived status + per-partition aggregation

#### Context

Stage 5 of the multi-release alignment plan. The audit completed in
the v0.78.12 conversation found three places where frontend derived
values from raw fields:

| Field | Where computed (before) |
|---|---|
| Backfill derived status (stuck-SFN override) | Frontend only (`utils/backfillStatus.ts`) |
| Per-partition aggregate status | Frontend only (`summarizePartition` in `BackfillDetailPage`) |
| Asset staleness (picker-scoped) | Frontend (`utils/staleness.ts`); backend separately computes its own |

Frontend-only derivation breaks when a second client appears (CLI,
mobile, webhook). Same logic re-implemented in each = drift risk.
This ADR moves backfill derivation to the backend per Mike's
alignment Q7/Q9.

(Q8 — `utils/staleness.ts` — handled differently. See "What this does
NOT do" below.)

#### Decision

**1. Server-side derived backfill status.**
`_compute_derived_backfill_status(item)` in `routes/backfill.py`.
Mirrors the (now-deleted) frontend `computeDerivedBackfillStatus`:

  - Terminal raw status → respected
  - `running`/`pending` + all partitions accounted for → derive
    `completed`/`failed`/`partial` from completed/failed ratio
  - Otherwise → raw

Applied in `_format_backfill_summary` (so it covers list + detail +
retry-chain links). The returned `status` field IS the derived value;
**raw is no longer exposed in API responses** (per Q7). If raw and
derived diverge, the helper logs at `warn` level with full context
(`backfill_id`, `raw_status`, `derived_status`) for CloudWatch
diagnosis of stuck SFNs.

**2. Server-side per-partition aggregate.**
`_summarize_partition_status(children, partition_key)` in
`routes/backfill.py`. Mirrors the frontend `summarizePartition`:

  - No children for this key → `pending`
  - All success → `success`
  - Any failure/upstream_failed/aborted → `failed`
  - Otherwise → `running`

Detail response gains a new field (per Q9):
```json
"partitions": [
    {"key": "2024-01-15", "status": "success"},
    {"key": "2024-01-16", "status": "failed"},
    ...
]
```

The frontend `BackfillDetailPage.partitionBadges` now reads
`detail.partitions` directly (with a legacy fallback to
`partition_keys` mapped to `pending` for cases where backend hasn't
redeployed yet).

**3. Frontend cleanup.**
- Deleted `ui/src/utils/backfillStatus.ts` and its test file.
- `BackfillsListPage` reads `b.status` directly (no client-side
  override).
- `BackfillDetailPage` reads `detail.status` and `detail.partitions`
  directly. The previous diagnostic `console.warn` for raw≠derived
  divergence is gone — that's now a `log.warn` in CloudWatch where
  ops people actually look.
- `isBackfillActive` becomes a tiny inline check against
  `BACKFILL_TERMINAL_STATUSES` (sourced from `@/generated/enums`,
  per ADR #72).

#### What this does NOT do

- **Does not delete `utils/staleness.ts`** as originally planned in
  the Q8 answer. On closer review the file has picker-scoped
  resolution logic (recent-events for the visible date range,
  fallback to backend-computed `last_updated` otherwise) that the
  backend doesn't replicate — backend computes staleness from a
  single timestamp without picker context. Deleting the util would
  lose the picker-scoped behavior. The original "delete entirely"
  recommendation was based on a shallower read of the file.
- **Does not migrate SFN templates.** The Finalize step in
  `bulk_backfill/sfn.tpl.json` writes the terminal status from
  JSONata, and that's a fine source of truth — it's the
  *stuck-without-Finalize* case the override catches. Templates stay.
- **Does not add per-partition retry/cancel endpoints.** That's
  Stage 6 (v0.79.2).

#### Tests

Backend (`tests/routes/test_backfill.py`, +15):

- `TestDerivedBackfillStatus` (8 tests):
  - Terminal status respected (per each terminal value)
  - Running + all completed → 'completed'
  - Running + all failed → 'failed'
  - Running + mixed → 'partial'
  - Running + some pending → 'running' (untouched)
  - `skipped` counts toward processed
  - Pending + no partitions → 'pending'
  - String-coerced int inputs (DDB returns Decimal sometimes)
- `TestSummarizePartitionStatus` (7 tests):
  - No children → 'pending'
  - All success → 'success'
  - Any failed/upstream_failed/aborted → 'failed'
  - In-flight (running) when not all done
  - Partition_key filter isolates correctly

Frontend tests rewritten for the backend-trust contract:

- `BackfillDetailPage.test.tsx` — `derivedStatus` describe block
  replaced with `status rendering trusts backend` (5 tests). Mocks
  provide the derived value (matches what backend returns).
- `BackfillsListPage.test.tsx` — stuck-running tests replaced with
  the backend-trust equivalents (same 5 tests, semantics aligned).

#### Lesson

When the same computation lives in N places, the fix isn't "find a
clever shared util" — that just hides the multiplicity at the
language level. The fix is to put the computation behind the API
boundary, so every client (including ones that don't exist yet)
gets the same answer for free. Frontend becomes a renderer, not a
deriver.

Equally important: when planning, "delete the duplicate util" sounds
clean, but you have to actually open the file. Q8 looked obvious in
the abstract; the real `staleness.ts` had picker-scoped logic the
backend doesn't replicate. Walking through the code before deleting
is part of the work, not optional rigor.
