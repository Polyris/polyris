---
area: engine
accepted: by 2026-05-29
---
# ADR #88 — Upstream smart-fill — implementation architecture (Python-planned, SFN-trusted)

#### Status

Accepted (implementation architecture). Builds on ADR #86 (asset-centric
UX, unchanged DSL) and ADR #87 (partition-mapping shape, Dagster prior
art). This ADR fixes *how* upstream smart-fill is built so the phased work
can proceed without re-architecting. Code not yet written.

#### Context — what the deep code analysis found

A read-through of the existing backfill flow established the constraints
the implementation must respect:

1. **The bulk-backfill SFN is intentionally "dumb"; Python plans.** Per the
   `_scan_completed_partitions` contract: the SFN trusts the partition list
   it is given and has no "check if done" state — all
   completeness/skip intelligence runs in Python at API request time. Any
   new planning intelligence (upstream resolution) must follow the same
   pattern: compute in Python at `start_backfill` time, hand the SFN an
   already-resolved plan.

2. **`_scan_completed_partitions` is the existence-check primitive.** It
   already answers "is partition D of pipeline P complete" (every expected
   task succeeded). Smart-fill reuses it verbatim to decide reuse-vs-build
   for an upstream partition.

3. **Same-pipeline upstream is free.** Within one pipeline the DAG already
   runs tasks in dependency order via `dependency_wrapper`. Upstream
   smart-fill is only needed when an upstream asset is produced by a
   *different* pipeline. Both cases occur in practice.

4. **Reusable building blocks exist:** `_expand_partitions` /
   `PartitionRange` (range → key list), `_resolve_target` +
   `_find_producers_for_asset` (asset → producing pipeline),
   `_floor_to_bucket` / `_advance` (bucket math), the `Initialize → Map →
   Finalize` bulk-backfill structure.

#### Decision

**Architecture (A): resolve in Python, keep the SFN trusting its plan.**

At `start_backfill` time, when `upstream != off`, a resolver computes the
full execution plan — target partitions plus any missing cross-pipeline
upstream partitions, arranged in dependency tiers — and the bulk-backfill
SFN executes that plan. The SFN gains no new planning logic; it remains the
trusted executor. This mirrors the established `skip_completed` pattern and
keeps SFN-change risk low (the class of failure that produced the v0.80.1
`bf-336b8481` hang).

**Two upstream scopes, handled distinctly:**

- *Same-pipeline upstream* → no resolver work. Backfilling the pipeline for
  date D already runs its internal DAG in dependency order. Free.
- *Cross-pipeline upstream* → the resolver walks the asset dependency graph
  upward across pipeline boundaries, maps each edge via
  `partitions_needed(asset, D)` (ADR #87 default = intersecting window),
  checks existence via `_scan_completed_partitions`, and schedules the
  missing upstream pipeline+partition before the target. In mode `smart`,
  existing upstream partitions are reused; in `force`, they are rebuilt.

**Resolver contract:** consumes only `partitions_needed(asset, D)`; has no
separate notion of window vs granularity (ADR #87). Includes cycle
detection and diamond dedup. Window offsets are declared-but-not-honored in
Phase 1 (warn), implemented in Phase 2 (ADR #87 phasing).

#### Open edge-case decisions (resolved here)

- **Stale-vs-reuse:** `smart` reuses an upstream partition iff it *exists*
  (passes `_scan_completed_partitions`); `force` rebuilds regardless. No
  freshness-comparison in Phase 1 (StalenessStatus stays a display concept;
  wiring it to reuse decisions is deferred — start simple, add later if a
  real case demands it).
- **Backfill-vs-scheduled conflict on the same date:** backfill wins. It is
  explicit user intent; a scheduled run for a partition under active
  backfill is suppressed/deferred. (Lock semantics detailed at build time.)
- **Future-date:** reject partitions beyond "now" floored to granularity,
  with a clear validation error.
- **max_parallel:** the cross-pipeline plan respects a parallelism cap to
  avoid SFN `StartExecution` throttling; concrete limit set against the
  existing concurrency guard at build time.
- **mark-partition-done:** lift the existing task-level `mark_success`
  (routes/tasks.py) to partition scope so a manually-fixed partition is
  treated as complete by future smart-fill (reuse, not re-run). Requires a
  confirmation in UI because it makes the partition reusable downstream.
- **Boundary (upstream shorter history):** return the in-range covering set
  and warn; never hard-fail (ADR #87 allow-nonexistent semantics).

#### Phasing

- **Phase 0:** this ADR (done).
- **Phase 0.5 (spike, done):** the cross-pipeline tiered-resolution mechanic
  was prototyped and proven on the real partition primitives — partition
  mapping (1↔1, cross-granularity), tiering, diamond dedup (incl.
  asymmetric), and cycle detection all hold. Outcome: the "one backfill_id,
  sequential tiers" approach is sound; the `force`-fallback (chained
  sub-backfills) is not needed. The spike was then removed (superseded by
  the production module) and its algorithm carried into Phase 1.
- **Phase 1 (done):** `polyris/upstream_resolver.py` (`resolve_plan`,
  `AssetGraph`, `AssetNode`, `PlanItem`, `ResolvedPlan`, `CycleError`) +
  `partitions.partitions_covering`. O(V+E) tiering via Kahn propagation
  (not the spike's re-walk), per-item `dag_hash` (R5), window-offset surface
  reserved with warn (R2). Pure SDK, fully tested.
- **Phase 2 (done):** `console_api/upstream_integration.py` builds the
  cross-pipeline `AssetGraph` from the registry and an `exists` adapter over
  `_scan_completed_partitions`; `start_backfill` accepts the top-level
  `upstream` option (off/smart/force, default off = unchanged behavior),
  resolves the plan, surfaces it in preview, and draws the honest Phase 2/3
  boundary: a real start needing cross-pipeline upstream *built* returns 422
  `upstream_execution_pending` with the plan rather than silently running on
  missing upstream.
- **Phase 1:** `partitions_needed()` + cross-pipeline upward graph walk
  (cycle detection, diamond dedup), existence via `_scan_completed_
  partitions`, intersecting-window default (1↔1 + cross-granularity).
  Same-pipeline upstream: no-op (DAG handles). Window offset: warn-only.
- **Phase 2:** integrate into `start_backfill` behind `upstream` option;
  default `off` preserves current behavior (backward-compatible).
- **Phase 3:** SFN — minimal-to-none under architecture (A); the Map
  executes the Python-built tiered plan. `make audit-jsonata` + a focused
  JSONata test gate any template touch.
- **Phase 4:** rename `cascade` → `downstream` with alias + deprecation
  (drift caught by `check-generate-enums`); backfill UI dialog
  (upstream/downstream); mark-partition-done.
- **Phase 5:** quality gates — new resolver/integration/compat tests added
  to the existing suites; no skipped tests (#11); version bump via
  `check-versions`.

#### Consequences

- SFN-change risk stays low: planning is Python-side, the SFN keeps
  trusting its input (consistent with `skip_completed`).
- Same-pipeline upstream costs nothing; only cross-pipeline upstream
  exercises the resolver — scope is proportional to actual cross-pipeline
  dependency use.
- `upstream=off` default means zero behavior change for existing backfills
  until a user opts in.
- The remaining build-time choices (lock semantics, max_parallel value,
  mark-done UX copy) are parameters, not architecture — they will not
  reopen this ADR.

#### CLAUDE.md alignment

#2 (one resolver, reuse scan/resolve/expand/bucket-math; no parallel
backfill system), #6 (this alignment precedes code), #7 (architectural ADR),
#9 (extend `start_backfill` behind a flag; not a patch-on-patch), #11 (tests
each phase), #12 (reuse `_scan_completed_partitions`, `_find_producers_for_
asset`, `_expand_partitions`, DAG ordering, Map structure; Dagster shape as
prior art).
