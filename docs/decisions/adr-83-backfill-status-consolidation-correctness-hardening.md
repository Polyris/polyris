---
area: engine
accepted: 2026-05-28
---
# ADR #83 — Backfill status consolidation + correctness hardening

#### Context

ADR #81 (raw-vs-derived status in the concurrency guard) and ADR #82 (a
stray `+ skipped` term reporting running backfills as terminal) were the
same failure twice: the "is this backfill done / what's its terminal
status" rule was implemented in multiple places and drifted. A focused
quality pass consolidates that rule and closes the residual correctness
gaps the v0.80.0 audit surfaced.

This bundles the three workstreams the maintainer approved (A/B/C):

#### A — Parity drift check (one rule, verified across runtimes)

The terminal-status rule lives in two runtimes that can't share code:
Python (console API derivation) and JSONata (the bulk_backfill SFN
Finalize state). Created `polyris/codegen/check_backfill_status_parity.py`
(`make check-backfill-parity`), which asserts the SFN Finalize JSONata
encodes the canonical rule — the canceled/completed/failed/partial
decision structure, and that it does NOT reference `skipped` in the
aggregate (the ADR #82 guard). It's the same pattern as the SFN
status-literal drift check (ADR #78), enforced via the SDK test suite
(`tests/sdk/test_backfill_status_parity.py`). The JSONata is the only side
that *can* drift now (see B); this catches it at CI time.

#### B — Single Python authority + `BackfillRecord` value-object

- **`polyris/backfill_status.py`** is the one Python authority for the
  rule: `finalize_status(completed, failed, *, canceled)` and
  `all_map_done(total, completed, failed)`. Both the console API derivation
  and (logically) the SFN Finalize implement this; the parity check pins
  the JSONata to it. The Python side can no longer drift because every
  consumer calls these functions.
- **`BackfillRecord`** (in `dal/backfills_repo.py`) is a typed, read-only
  view over a Backfill DDB item. It centralizes field access and the
  status/counter semantics: `id` (backfill_id or execution_name),
  `total`/`completed`/`failed`/`skipped` (typed, with the documented "total
  excludes pre-flight skipped" semantics), `raw_status`, `is_active`,
  `is_terminal`, `map_done`, `derived_status()`, `age_seconds()`. No
  consumer reads raw dict keys or re-derives done/active ad-hoc anymore —
  the exact pattern that drifted in ADR #81/#82.
- `_compute_derived_backfill_status`, `_reconcile_backfill_status`,
  `_heal_backfill_status` were refactored to go through `BackfillRecord`;
  the standalone `_backfill_age_seconds` and the duplicated
  `item.get('backfill_id') or item.get('execution_name')` (6 sites)
  collapsed into the record. `_compute_derived_backfill_status` is kept as
  a thin shim (delegates to `BackfillRecord.derived_status`) so existing
  callers/tests are undisturbed.

#### C — Correctness gaps from the audit

- **Child execution name length (fixed).** The bulk_backfill SFN names
  each child `{pipeline}-{partition}-bf-{hex8}`; AWS caps execution names
  at 80 chars. A long pipeline name plus a long partition key could exceed
  it, which would make StartExecution fail *inside the Map* — every such
  partition silently marked failed with no actionable error. `start_backfill`
  now validates the projected name length up front and rejects with
  `child_name_too_long` (422) and a clear message.
- **Hourly partition keys (verified safe, not a bug).** The hourly format
  `YYYY-MM-DDTHH` contains only SFN-name-legal characters; the earlier
  audit note was over-cautious. The only real edge was length, covered
  above.
- **`.sync:2` child Retry name reuse (documented, deliberately not
  edited).** The `StartChildSFN` Retry likely re-attaches to the same
  already-failed execution (idempotent StartExecution on constant
  name+input), making child-level retry ineffective. Impact is low
  (partitions still end correctly failed; backfill-level retry-failed is
  the real re-run path; cost is wasted backoff). Fixing it means editing
  orchestrator JSONata, which has no CI-side syntax validation and can't be
  end-to-end tested here. Per the "highest quality" bar, an unverified
  change to the most critical component is not shipped blind — the issue,
  its open AWS-behavior question, and two candidate fixes are specified in
  STATE.md for a deploy-time validation.

#### One source for the backfill status sets

The terminal and active backfill status sets have one canonical source, `polyris/constants.py`, which codegen carries to every consumer as `BACKFILL_TERMINAL_STATUSES` and `BACKFILL_ACTIVE_STATUSES`; `console_api/constants.py` re-exports them, and no hand-kept copy (`BackfillStatus.TERMINAL`, `.ACTIVE`, `.ALL`) remains. The string-value `BackfillStatus` class stays in `console_api` as the readable value namespace, covered by the enum drift check.

#### Why this is a minor version bump

New public SDK module (`polyris.backfill_status`), a new CI check, and an
internal architecture change (value-object + single authority). No API
contract change and no behavior change for valid inputs — the consolidation
preserves semantics while removing the drift surface.

#### Lesson

The recurring fix across #81, #82, #83 is the same sentence: *one rule, one
place, every consumer routes through it.* #81 and #82 patched the symptoms
(guard reads derived; remove the bad term). #83 removes the cause — there
is now exactly one Python authority, one typed accessor per concept, and a
CI check that the second runtime (JSONata) stays in parity. Drift is no
longer possible silently; it fails a test.

#### DAL placement note

`BackfillRecord` lives in `dal/backfills_repo.py` — the typed shape of what
the repo returns, co-located with it for cohesion. Its `derived_status()`
is a thin adapter that delegates to the `polyris.backfill_status` authority
(no logic ownership in the DAL). This is a deliberate judgment call given
there is no separate `models/` layer in console_api; if stricter layering
is wanted later, the value-object extracts cleanly to a models module
without touching its consumers.

#### Follow-up 2: models layer + class-level enum consolidation (same release)

Closing the two quality nuances flagged in review:

**DAL → models.** `BackfillRecord` was extracted from `dal/backfills_repo.py`
to a new `models/` layer (`models/backfill_record.py`). The DAL is now
persistence-only; the domain value-object (status/counter semantics) lives
in models. Consumers import `from models import BackfillRecord`; the DAL no
longer carries the domain helpers. Its tests moved to
`tests/models/test_backfill_record.py`.

**Class-level enum SSoT — BackfillStatus + AssetOperator.** Beyond the
terminal/active *sets* (Follow-up 1), the `BackfillStatus` *class* itself
was a manual duplicate in `console_api/constants.py` of the
codegen-generated class — and the drift check only validates
`constants_generated`, so the manual class was unguarded. Verified the
manual and generated values were identical for `BackfillStatus` and
`AssetOperator`, then replaced both manual classes with re-exports of the
generated ones. Per-value documentation was moved to the canonical
`polyris/constants.py` source. Now there is one definition per class,
guarded by the drift check.

**Found but deferred — three families have diverged.** `TaskStatus`,
`TriggerRule`, `PipelineStatus` could NOT be consolidated the same way:
their membership has already drifted (generated `TaskStatus.SUCCEEDED` not
in manual; manual `TriggerRule.DEFAULT` and `PipelineStatus.PAUSED/ABORTED`
not in canonical). A naive re-export would break those consumers. Reconciling
canonical membership first — and investigating whether canonical
PipelineStatus is genuinely missing paused/aborted (a possible latent bug,
not mere duplication) — is logged in STATE.md as the remaining piece of
the 8-family enum SSoT effort. Consolidating only the safe families now,
and documenting the unsafe ones precisely, is the correct bounded scope:
shipping a re-export that breaks `PipelineStatus.PAUSED` to chase tidiness
would trade a cosmetic SSoT win for a real regression.

#### Follow-up 3: TaskStatus / TriggerRule / PipelineStatus consolidation

Investigated the three families flagged as "diverged" and found the
divergent members were all **dead code**, not real gaps:
- `TriggerRule.DEFAULT`, `TriggerRule.EARLY_TRIGGER`, `TriggerRule.WAIT_ALL`
  — zero references repo-wide.
- `PipelineStatus.PAUSED`, `PipelineStatus.ABORTED` — zero references.
  (ABORTED is correctly an `ExecutionStatus` member already; the canonical
  `PipelineStatus` is intentionally the aggregate sidebar status —
  idle/running/succeeded/failed/waiting — not execution/operational state.
  So this was not a missing-status bug.)
- `TaskStatus` — canonical is a strict superset of the manual copy (only
  adds `SUCCEEDED`), so re-export is non-breaking.

Result: **console_api/constants.py now defines no duplicated status
classes.** TaskStatus, TriggerRule, PipelineStatus, BackfillStatus,
AssetOperator and the TASK_*/BACKFILL_* sets are all re-exported from the
codegen-generated module (the single source). Only console-specific
constants remain hand-written (EventType, Limits, BackfillLimits,
SFN_STATUS_MAP, validators). The dead members were dropped (CLAUDE.md #1).

**_shared / evaluate_deps** (the other copies of the "4-copy" problem):
`_shared/constants.py` keeps its status classes manual on purpose — it has
an ImportError fallback so the file works standalone in unit tests, and
re-exporting the generated classes would couple it hard to
`constants_generated` at import time. Rather than trade that resilience for
tidiness, the dead members (`DEFAULT`, `EARLY_TRIGGER`) were removed and a
new guard — `polyris.codegen.check_shared_constants` (run by
`make sync-constants`) — verifies the _shared manual classes never define a
value absent from canonical. So the duplication that remains is now
*guarded*: it cannot silently drift. evaluate_deps stays a verified copy of
_shared.

**sync-constants check fixed.** Its old text-line comparison (console_api
literals ⊇ _shared literals) broke once console_api stopped defining the
constants as literals (it re-exports now). Replaced with the value-based
`check_shared_constants` guard, which reflects the new structure and is the
more meaningful invariant.

This closes the enum-SSoT work for the status families touched by the
backfill review. The remaining families' string-value classes that are
still hand-written in _shared are now guarded rather than free-floating;
fully folding _shared into generated (resolving the standalone-resilience
tension) is the last piece of the broader effort, left as a deliberate
design decision in STATE.md.
