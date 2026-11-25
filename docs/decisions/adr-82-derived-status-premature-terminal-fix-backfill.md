# ADR #82 — Derived-status premature-terminal fix + backfill audit (v0.79.10)

#### Context

A full review of the backfill subsystem (routes/backfill.py, backfills_repo,
the bulk_backfill SFN template) after the v0.79.9 zombie-reconciliation
work surfaced one real correctness bug and a consistency gap.

#### Bug — `skipped_partitions` inflated the "processed" count

`_compute_derived_backfill_status` computed
`processed = completed + failed + skipped` and reported terminal once
`processed >= total_partitions`. But the counters mean different things:

- `total_partitions` is set to `len(partition_keys)` **after** the
  skip_completed pre-flight filter — it is the to-RUN count, excluding
  pre-skipped partitions.
- The bulk_backfill Map increments only `completed_partitions` and
  `failed_partitions`, one per iteration.
- `skipped_partitions` is the pre-flight skip_completed count, set once at
  record creation and **never** touched by the SFN.

So adding `skipped` to `processed` mixed a pre-flight count (not in `total`)
into the Map-progress sum. A partially-run backfill could be reported
terminal while the Map was still running. Example: backfill 8 partitions,
3 already complete → `total=5, skipped=3`; after 2 of 5 ran,
`processed = 2 + 0 + 3 = 5 >= 5` → "completed" while 3 were still in flight.

Before v0.79.9 this was a cosmetic display bug (list showed a running
backfill as done). v0.79.9 made it dangerous: `_reconcile_backfill_status`
drives the concurrency guard and self-heal, so a premature-terminal
derivation would heal a *running* backfill to terminal and drop it from
the active set — letting a concurrent backfill start against the same
pipeline and defeating the duplicate-write protection the guard exists for.

The SFN's own Finalize JSONata never had this bug — it computes status
from completed/failed only. The Python derivation diverged from it.

**Fix:** `processed = completed + failed`. "All Map work done" is exactly
`completed + failed >= total_partitions`. `skipped_partitions` stays in the
record for display ("N already complete") but no longer feeds the
done-check. The buggy test `test_skipped_counts_toward_processed` — which
had codified the wrong behavior — was rewritten to assert that a pre-flight
skip does not make a running backfill look terminal.

#### Consistency fix — retry eligibility uses reconciled status

`retry_failed` checked the raw stored status for eligibility
(failed/partial). A zombie parent stored `running` but effectively
failed/partial would be rejected as `not_eligible`, even though the UI
(showing the reconciled status) offers a Retry button. Routed the
eligibility check through `_reconcile_backfill_status(parent)` so the
backend agrees with what the user sees; reconcile also self-heals the
parent in passing.

#### Audit notes (reviewed, no change needed)

- **SFN sets `is_backfill: true`** on every child execution (both pipeline
  and asset targets go through bulk_backfill) — the old "asset-backfill
  missing is_backfill" concern is not present on this path.
- **No double-count of failures** — the StartChildSFN `Catch` (exception
  path) and the `ChildSucceededChoice` default (non-exception failed path)
  are mutually exclusive, so `IncrementFailed` fires at most once.
- **Canceled finalization** is correct — Finalize honors a `canceled`
  status regardless of counters.
- **DAL discriminators** (`record_type='backfill'`, sentinel pipeline_name)
  are set consistently in put/put_if_new and filtered on read.
- **No client-side derivation duplicate** — `backfillStatus.ts` was removed
  in v0.79.1; the UI consumes the backend status (CLAUDE.md #2 clean).

#### Audit notes (logged for backlog, not fixed here)

- **Hourly partition keys in child execution Name** — the SFN builds the
  child execution name from `partition_key`; if hourly granularity ever
  produces keys containing characters illegal in SFN execution names
  (e.g. `:`), StartExecution would fail. Daily/weekly/monthly keys are
  safe. Verify the hourly partition-key format before enabling hourly.
- **`.sync:2` Retry name reuse** — StartChildSFN retries reuse the same
  execution Name+Input; SFN StartExecution is idempotent on name+input, so
  the Retry block may not produce a genuinely fresh attempt. Pre-existing;
  warrants a live test before relying on child-level retries.
- **`force` / `incremental` options** remain accepted-but-unused (API
  compat, documented). Borderline dead config; kept intentionally.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| console_api | 355 | **356** | +1 net (rewrote derived-skip test; added retry-zombie-eligible) |
| SDK Python | 912 | 912 | 0 |
| **All gates** | green | **green** | — |

#### Lesson

Two helpers (`_compute_derived_backfill_status` and the SFN Finalize
JSONata) implemented the same concept — "is this backfill done?" — and
drifted: the JSONata used completed/failed, the Python added skipped. When
the same rule lives in two places it will diverge; the safest design has
one authority. Here the SFN must own the Finalize math (it runs in the
execution), so the Python derivation's job is only to *mirror* it — and a
shared comment now pins them together. The same drift caused ADR #81 (raw
vs. derived status in the guard); the recurring fix is "one rule, one
place, every consumer routes through it."
