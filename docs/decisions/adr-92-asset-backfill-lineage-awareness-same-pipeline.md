---
area: engine
accepted: by 2026-05-29
---
# ADR #92 — Asset backfill lineage-awareness (same-pipeline frontier) + correction of ADR #88

#### Context

ADR #88 claimed "same-pipeline upstream is free — the DAG handles it." That
is true for a *pipeline* backfill (the child runs the full pipeline DAG in
dependency order), but NOT for an *asset* backfill: `_resolve_target` sets
`task_subset = [producer_task]`, so `_compute_skip_task_ids` skips every
other task — including the producer's own same-pipeline ancestors. The
producer then runs reading its inputs from the canonical-output store
(`output#{pipeline}#{task}#{date}` in pipeline-tokens, written by
`Save_Canonical_Output`, read by `Read_Upstream_Outputs`). If those inputs
are missing for the partition, the asset is built on empty/stale input — the
"green cell over red upstream" failure visible in the Asset Matrix.

Empirical findings grounding the fix:
- Every task writes a canonical output on success; existence of an
  ancestor's output for a partition is observable via the SAME signal
  `_scan_completed_partitions` already uses (`executions_repo.query_by_date`
  → per-task status; one query returns all tasks for a (pipeline, date)).
- Canonical outputs carry a TTL of ~31 days (dependency_wrapper:
  `max(orchestration_timeout, 2592000) + 86400`). Beyond that window an
  ancestor's output is gone; a `skip_on_backfill` ancestor (e.g. an extract
  that pulls live data) cannot be rebuilt, so deep-historical partitions can
  be genuinely unbuildable. This is a data boundary, surfaced — not hidden.
- Prior art: Dagster supports backfilling a whole lineage tree and has a
  distinct "only missing partitions" mode; dbt's `+model` selects upstream
  and `state:modified` flags stale-by-code. Validates the smart/force split
  and the upstream-fail-blocks-downstream concern (already handled by the
  Phase-3 tier gate, ADR #90).

#### Decision

Asset backfill gains a top-level `upstream` mode (mirrors the existing
`downstream`), governing how much of the producer's **same-pipeline**
lineage is (re)built. A single shared frontier walk powers it:

```
frontier(producer, requested_partitions, status_of, force):
  run = {producer}
  for each direct dependency d of a task already in `run`:
    if d.skip_on_backfill:        # never run; downstream reads stored output
        continue
    if force:                     run += d; recurse into d's deps
    elif d's output is MISSING for ≥1 requested partition (status_of):
                                  run += d; recurse into d's deps
    else:                         # output present for all → stop (read it)
        continue
  return run            # -> task_subset, fed to existing _compute_skip_task_ids
```

- **off** (default, unchanged): `task_subset = [producer]`. Bug-fix re-run;
  inputs assumed present. Backward-compatible.
- **smart**: producer + ancestors whose output is missing for at least one
  requested partition (union across partitions — Phase A carries one skip
  set for all partitions, so the union never under-runs; re-running an
  already-present ancestor is idempotent). Frontier stops at present outputs.
- **force**: producer + all non-`skip_on_backfill` ancestors, regardless of
  existing outputs (full lineage rebuild).

Reuse and limits:
- Existence uses `executions_repo.query_by_date` (one query per partition,
  memoized) — the proven completeness signal; no new DAL access pattern.
- Bounded by `PREFLIGHT_MAX_PARTITIONS` (as skip-completed is); beyond it,
  `smart` degrades to `force` with a warning (never under-runs).
- `skip_on_backfill` ancestors are never added to the run set, preserving the
  read-from-storage pattern. Preview warns when such an ancestor's stored
  output is missing/expired for a requested partition (the TTL boundary).
- `_compute_skip_task_ids` is unchanged — the frontier only widens the
  positive `task_subset` fed into it (#9 widen, don't patch).

Granularity: not a concern in Phase A — a single pipeline runs all its tasks
for one partition key, so the whole same-pipeline lineage shares one
granularity by construction. (Cross-pipeline granularity is Phase B's, where
ADR #87 `partitions_covering` already maps.)

Deferred (recorded, not built):
- **Cross-pipeline per-item lineage (Phase B):** each tiered plan item
  carries its own `skip_task_ids` (its asset's frontier), composed onto the
  Phase-3 nested-Map. Requires a per-item SFN-input change. Trigger: a second
  pipeline exists. Until then there is no consumer, so no code (#1).
- **Stale-by-code:** `smart` keys off output *existence*, not freshness; an
  existing-but-stale output (changed task code) is reused. `force` rebuilds.
  The signals (`dag_hash` per partition, `updated_at`) exist for a later
  freshness mode.

#### Consequences

- Asset backfill can build an asset correctly from its real same-pipeline
  lineage, not just re-run the producer on assumed-present inputs.
- The common single-pipeline deployment is fully served with **no SFN
  change** (safe deploy).
- Preview must disclose scope (tasks-to-run × partitions, reused,
  skip_on_backfill+TTL warnings) so a one-cell click that expands to a large
  lineage is never a surprise.
