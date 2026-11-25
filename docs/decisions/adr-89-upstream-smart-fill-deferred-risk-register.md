---
area: engine
accepted: by 2026-05-29
---
# ADR #89 — Upstream smart-fill — deferred-risk register (no-surprises contract)

#### Status

Accepted (risk register). Companion to ADR #88. Its purpose is explicit:
enumerate every known risk in the upstream smart-fill design and give each
a concrete resolution — decided-now, or deferred-with-a-named-trigger-and-
plan — so that no risk is discovered late as a surprise. "Detailed at build
time" is not used here; each item states the decision and, critically, what
data we capture from day one so that a later policy change needs no
retroactive migration.

#### Principle

The cheapest insurance against a future rework is to **capture the data a
future decision would need, even if we don't act on it yet.** A policy can
change cheaply; a missing column across historical records cannot. So
Phase 1 records more than it uses.

#### Risk register

**R1 — Reusing a stale upstream partition yields wrong data.**
- Decision (Phase 1): `smart` reuses an upstream partition iff it exists
  (`_scan_completed_partitions`); `force` rebuilds regardless. The `force`
  mode is the always-available escape hatch.
- Data captured now: each reused partition's producing `dag_hash` and
  `last_updated` are already on the token rows; the resolver records, on the
  backfill, which upstream partitions were reused vs built.
- Deferred: a third mode `upstream=fresh` that compares freshness
  (`StalenessStatus` / `freshness_hours`, both already exist) before reuse.
- Trigger to implement: first real incident of "reused stale → wrong data",
  or a user asking for freshness-gated reuse. Additive (new enum member),
  no migration.

**R2 — Window-offset DSL surface, added in Phase 2, breaks early adopters.**
- Decision (now, to remove the risk): reserve the surface immediately even
  though Phase 1 does not honor it. The shape is
  `inlets=[asset.offset(start=-6, end=0)]` (mirrors Dagster
  `start_offset`/`end_offset`; units follow the asset granularity). Phase 1
  parses and validates it but warns "window offset not yet honored at the
  orchestration layer; resolves as 1↔1". Phase 2 implements expansion only.
- Result: Phase 2 is genuinely additive — the authored API does not change,
  only its runtime effect turns on. No early-adopter breakage.

**R3 — Tiered cross-pipeline plan mechanics are unproven.**
- Decision (Phase 1 target, to be confirmed by spike): ONE `backfill_id`
  carrying a resolved tiered partition plan; the existing `Map` runs tier
  N+1 only after tier N completes (sequential tiers, parallel within a
  tier, bounded by MAX_PARALLEL).
- Fallback if the spike shows this doesn't hold: chained sub-backfills
  (upstream pipeline backfilled first, target after), each a normal
  bulk-backfill, linked by parent `backfill_id`.
- Mandatory de-risk: a 2–3 day **spike on a branch** against one real
  cross-pipeline case BEFORE locking the mechanic. This is the single
  genuinely unknown piece; the spike exists to find the wall before code.

**R4 — mark-partition-done has no un-mark / downstream invalidation.**
- Decision (Phase 1): mark-partition-done is one-way and requires UI
  confirmation (it makes the partition reusable downstream). We are not
  regressing — no invalidation exists anywhere today.
- Deferred: invalidation (un-mark a partition + cascade-invalidate
  downstream consumers that reused it) as its own feature/ADR.
- Trigger: first need to undo a wrongly-marked partition. Until then,
  recovery path is `force` rebuild of the affected partition + its
  downstream — which already works.

**R5 — A partition is "valid" but was built with since-changed code.**
- Good news from code: `generate_dag_hash` exists and backfill already
  stores `pipeline_dag_hash`. Detection is therefore possible today.
- Decision (Phase 1): record the producing `dag_hash` per partition (cheap;
  the hash is already computed at deploy and loaded by `LoadPipelineDagHash`).
  In `smart` mode, if a reusable partition's `dag_hash` differs from the
  current pipeline's, **warn** ("partition exists but built under an older
  pipeline version") — do not block.
- Deferred: code-version-aware auto-rebuild policy.
- Trigger: a user wanting "rebuild anything built under old code". Because
  the hash is captured from day one, this becomes a pure policy addition —
  no retroactive backfill of missing data.

**R6 — Two backfills contend for the same upstream partition.**
- Decision: reuse the existing per-pipeline active guard
  (`list_active_for_pipeline`, `allow_concurrent=False` default). When
  smart-fill would schedule an upstream pipeline+partition that is already
  in flight, the resolver treats it as in-progress: it waits on / defers to
  the active run rather than starting a duplicate. Mechanism (active guard)
  is decided; the wait-vs-skip detail is a resolver parameter, not new
  architecture.

**R7 — Cancel during a partial backfill that already built some upstream.**
- Decision: reuse existing `cancel_backfill` semantics. Upstream partitions
  already built are real, valid data and remain reusable by future
  smart-fill. Target partitions partially done → `partial`. In-flight
  children honor the existing cancel path. No new state is introduced;
  built-upstream simply persists as completed partitions.

#### Phase 0.5 — the spike (added to ADR #88 phasing)

Before Phase 1 implementation, run a 2–3 day branch spike exercising R3
(tiered cross-pipeline plan) end-to-end on one real pipeline pair. The
spike's only job is to confirm or refute the "one backfill_id, sequential
tiers" mechanic and surface any unknown before it is encoded. Outcome
updates ADR #88's Phase-3 decision.

#### What remains genuinely open (and is acceptable)

After this register, the only un-pre-decided item is R3's exact mechanic,
which is precisely why it gets a spike. Everything else is either decided or
deferred with a named trigger and zero-migration data capture. This is the
no-surprises contract: future change lands as additive policy on data we
are already recording, confined to the resolver + backfill integration —
never the concept (ADR #86), the mapping shape (#87), or the DSL.

#### CLAUDE.md alignment

#6 (risks aligned before code), #7 (decisions recorded), #9 (deferrals are
additive, not patch-on-patch), #12 (reuse dag_hash, active guard, cancel
semantics, StalenessStatus rather than building new). #8 no-stubs is
honored by the spike: we prove the one unknown mechanic rather than stubbing
it.
