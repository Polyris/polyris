---
area: engine
accepted: 2026-05-28
---
# ADR #81 — Zombie-backfill reconciliation unblocks the concurrency guard

#### Context

Users could not start a backfill from the asset tab (or pipeline tab):
every attempt returned `409 concurrent_backfill_active`, even though the
matrix showed `0 running` executions. The nav badge read "Backfills 1" —
one backfill the system considered active, permanently blocking all new
backfills for that pipeline.

Root cause: a **zombie backfill**. The `bulk_backfill` SFN writes
`status='running'` at start and stamps the terminal status in its
Finalize step. If Finalize never runs (SFN timeout, abort, mid-way
crash, or an orphaned record whose SFN never started), the DDB record
stays `running`/`pending` forever. The concurrency guard
(`list_active_for_pipeline`) checks the **raw stored status**, so a dead
backfill blocks every new one — including asset-tab backfills, which
resolve to the asset's producing pipeline and hit the same guard.

The codebase already had `_compute_derived_backfill_status` (v0.79.1,
ADR #73) that detects the "all partitions done but not finalized" case —
but it was used only for **display**, never for the guard. So the UI
could show a backfill as completed while the guard still treated it as
active. A pure mismatch between what's shown and what's enforced.

#### Decision

Add a single reconciliation helper, `_reconcile_backfill_status(item)`,
used wherever a backfill's *effective* status matters. It resolves a
pending/running record cheapest-first and heals zombies in place:

- **A — counters (no AWS call).** Reuse `_compute_derived_backfill_status`.
  Covers the common zombie: all partitions processed, Finalize just
  didn't run → terminal.
- **C — SFN execution state.** If counters are inconclusive, describe the
  bulk_backfill execution (its name equals the `backfill_id`). A terminal
  execution (`SUCCEEDED`/`FAILED`/`TIMED_OUT`/`ABORTED`) — or a missing
  one (`ExecutionDoesNotExist`: never started or aged out of SFN's 90-day
  history) — means the backfill cannot still be running.
- **B — self-heal.** When A or C resolves a pending/running record to a
  terminal status, stamp it in DDB via a conditional update (status still
  pending/running). Idempotent; concurrent heals are safe (the loser hits
  ConditionalCheckFailed and no-ops). Best-effort: a heal failure never
  breaks the caller.

Two call sites, one helper (no duplication):
- **Concurrency guard** (`start_backfill`): filter the raw-active list
  through reconciliation; only genuinely pending/running backfills block.
  Zombies heal and drop out → new backfills proceed.
- **Display** (`_format_backfill_summary`, used by list + detail): report
  the reconciled status. Side effect: the "active backfills" badge and
  the guard self-heal on the next list/detail load, without a manual
  cancel.

SFN→Backfill terminal mapping: `SUCCEEDED→completed`, `FAILED`/
`TIMED_OUT→failed`, `ABORTED→canceled`, missing execution→`failed`.

#### Edge case: the just-started race (age guard)

`ExecutionDoesNotExist` is ambiguous. It means *either* "the SFN
`start_execution` for a just-created backfill hasn't propagated yet"
(the record is seconds old and legitimately starting) *or* "the
execution never started / aged out of SFN's 90-day history" (a genuine
zombie). Healing the first case to `failed` would let a concurrent
reconcile (a list load, or a second start hitting the guard) kill a
backfill that is starting normally — `start_backfill` writes the
`pending` record (`put_if_new`) *before* `sfn.start_execution`, so there
is a real window where the record exists but the execution is not yet
describable.

Resolution: the missing-execution path heals only when the record is
older than `_EXECUTION_GONE_MIN_AGE_SECONDS` (120s). SFN executions are
describable within milliseconds of `start_execution` returning, so any
record older than that with no execution is certainly orphaned; anything
younger is treated as still active (returned as raw, not healed). The
counters path (A) and the SFN-terminal path (FAILED/TIMED_OUT/ABORTED)
do not need the guard — counters only complete if work actually ran, and
a terminal execution definitively ended. `started_at` is stamped at
record creation, so age is always available for real records; if it is
somehow absent, the path errs toward *not* healing (treats as active),
preserving the race-safety bias.

#### Fail-closed

On any *unexpected* SFN error during reconcile (throttling, IAM), the
helper returns the raw status — the backfill stays "active" and the guard
still rejects. Wrongly blocking a new backfill is safer than allowing a
true duplicate (the guard exists to prevent duplicate per-partition
writes). Only `ExecutionDoesNotExist` is treated as a definitive
not-running signal.

#### Why reconcile on read (display) too, not just the guard

The guard fix alone unblocks new backfills, but the zombie record would
linger as `running` in DDB and the "Backfills N" badge would stay wrong
until someone started a new backfill against the same pipeline. Healing
on read (the lazy-reconciliation pattern, same as pipelines'
`_reconcile_running`) means any list/detail view clears the badge. The
cost is bounded: reconciliation short-circuits at A (no AWS call) for the
common case, and only running/pending records ever reach the SFN describe
— terminal records return immediately.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| console_api | 348 | **355** | +7 |
| — `TestZombieReconciliation` | — | 6 | counters-heal, SFN-timeout-heal, execution-gone-heal (old record), just-started-not-healed (race guard), genuinely-running-blocks, SFN-error-fails-closed |
| — `TestListBackfills.test_list_self_heals_zombie_status` | — | 1 | display self-heal + badge |
| SDK Python | 912 | 912 | 0 |
| **All gates** | green | **green** | — |

The pre-existing concurrency reject test was tightened to set an explicit
`describe_execution → RUNNING`, so it no longer passes by accident on a
truthy MagicMock.

#### What this does NOT do

- **Does not change the `bulk_backfill` SFN.** The Finalize step is still
  the primary path that stamps terminal status; reconciliation is the
  safety net for when it doesn't run.
- **Does not interrupt in-flight executions.** Healing only re-labels the
  parent record; it never stops a real running SFN. A genuinely-running
  backfill (SFN status RUNNING) is never healed.
- **Does not add a background sweeper.** Reconciliation is lazy
  (on-guard, on-read). A stuck backfill on a pipeline nobody ever lists or
  backfills again stays labeled running — harmless, since it blocks
  nothing until someone tries that pipeline (at which point it heals).

#### Lesson

The bug was a split between *computed* truth and *enforced* truth. v0.79.1
correctly derived the real status for display but left the guard reading
raw — so the system showed one thing and enforced another. When a value
has a "real" derivation, every consumer that acts on it (display AND
guards AND counts) must go through the same derivation, or they drift.
The fix routes all three through one `_reconcile_backfill_status`.
