# ADR #90 — Upstream smart-fill Phase 3 — tiered SFN execution (nested Map + tier gate)

#### Status

Accepted. Implements Phase 3 of ADR #88: make the bulk-backfill SFN execute
a Python-built tiered cross-pipeline plan. Builds on Phases 1–2 (resolver +
integration, v0.81.0). Touches live orchestration, so it follows the
JSONata discipline of ADR #85 (jsonata-python proof + `make audit-jsonata`).

#### Context

After Phase 2, `start_backfill` resolves a tiered plan but refuses to
execute cross-pipeline upstream (honest 422 `upstream_execution_pending`).
Phase 3 removes that refusal by teaching the SFN to run the plan.

The current SFN runs ONE pipeline across a flat `partition_keys` list via a
single Map. Two things must change: (1) items must span pipelines (each
carries its own `sfn_arn`), and (2) tiers must run in dependency order
(deepest upstream first), while items within a tier still run in parallel.

A subtlety surfaced during design: sequential tiers guarantee *order* but
not *dependency gating*. If an upstream partition fails, the dependent
target must not run on missing/incomplete data (the silent-wrong-data
failure mode this whole effort guards against).

#### Decision

**Execution mechanic — nested Map.**
- Outer Map over `input.tiers` with `MaxConcurrency: 1` → tiers run
  strictly sequentially, in order (AWS Map with concurrency 1 preserves
  order).
- Inner Map over the tier's items with `MaxConcurrency: max_parallel` →
  items within a tier run in parallel, exactly as the pre-Phase-3 single
  Map did.
- The inner item processor is the existing child-execution logic
  (cancel-check → StartChildSFN → increment), reused. `StartChildSFN`
  already reads `target_pipeline_sfn_arn` per item, so cross-pipeline items
  work by carrying their own arn.

**Unified input (no dual path) — CLAUDE.md #2.** The SFN input becomes
`tiers: [[item, ...], ...]` where each item is
`{sfn_arn, pipeline, partition_key, reused}`. The single-pipeline case
(`upstream=off`) is simply a one-tier plan, so there is no separate legacy
code path. `start_backfill` and `retry_failed` both build tiers via one
helper. The DDB record keeps `partition_keys` (the target partitions, for
display / retry) and `total_partitions` now counts only executable
(non-reused) items.

**Reused-skip.** An item with `reused: true` (smart-fill found the upstream
partition already complete) is not executed — a Choice routes it to a Pass.
Reused items do not increment completed/failed (they are not work).

**Tier gate (failure safety).** At the start of each tier, the processor
re-reads the backfill record. If `status = canceled` OR
`failed_partitions > 0`, the entire tier is skipped. Because nothing has
run when the first (deepest) tier starts, `failed_partitions = 0` and it
runs; if any item in a tier fails, every later tier is skipped. This
guarantees a downstream/target is never run on top of a failed upstream —
no silent wrong data. The gate is a Choice on an integer read from DDB (the
same shape as the existing cancel-check), not a type-coercing expression,
keeping JSONata risk low.

This gate is deliberately **coarse**: a single upstream failure blocks all
later tiers, including targets whose own upstream actually succeeded (or was
reused). That is the safe default. Per-partition gating (a target runs iff
*its* upstream partitions succeeded) is a refinement — recorded as Phase 3b
below — not shipped now, because doing it correctly requires per-item
`upstream_keys` plus a multi-partition completeness check in the SFN, which
is exactly the array-JSONata risk class (ADR #85) and warrants its own
careful pass.

**Finalize unchanged.** The aggregate-status computation (canceled /
completed / failed / partial from completed+failed counters) is untouched,
so `check-backfill-parity` stays green and the canonical
`backfill_status.finalize_status` rule still governs.

#### Phasing within Phase 3

- **3a (this change):** nested-Map tiered execution, reused-skip, coarse
  tier gate. Cross-pipeline upstream now executes; downstream never runs on
  a failed upstream (coarsely). Phase 2's `upstream_execution_pending` 422
  is removed.
- **3b (deferred, trigger = a real case where coarse gating blocks too
  much):** per-partition gating via item-carried `upstream_keys` + a
  completeness check. Additive; revisits only the gate, not the mechanic.

#### Consequences

- Cross-pipeline upstream smart-fill is end-to-end functional.
- The common path (`upstream=off`, single tier) is behaviorally identical:
  one tier, gate passes at failed=0, inner Map runs all partitions in
  parallel — same as before.
- SFN-change risk is contained: the risky child logic is reused; new
  surface is the outer tier Map, the reused Choice, and the integer tier
  gate, all proven with jsonata-python and swept by `make audit-jsonata`.
- Coarse gating is a known, documented limitation with a named trigger for
  3b — no surprise.

#### CLAUDE.md alignment

#2 (unified tiers input, reused inner processor — no dual path), #6 (this
ADR precedes code), #7 (architectural decision recorded), #8 (no stubs —
3a is complete and correct, 3b is a separate honest scope, not a stub), #9
(extends the SFN; the coarse gate is a deliberate safe design, not a
patch), #11 (template + integration tests, JSONata proof), #12 (reuses the
child-execution processor, Finalize, cancel-check pattern, the resolver).
