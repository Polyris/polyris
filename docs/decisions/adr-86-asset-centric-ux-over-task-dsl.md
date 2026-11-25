# ADR #86 — Product concept: asset-centric UX over an unchanged task-based DSL

#### Status

Accepted — foundational product decision. Locks the conceptual model so we
develop forward instead of re-architecting. Supersedes the ad-hoc drift
between "pipeline backfill" vs "asset backfill" and the overloaded
`cascade` parameter.

#### Context

The codebase had accumulated two parallel mental models — task/pipeline
and asset (Dagster-style) — both surfaced as first-class
to the user. This produced concept sprawl: two backfill entry points,
`cascade` (auto/all/none) bolted onto only asset targets, `skip_tasks` /
task-subset, skip-completed pre-flight, and a UX that forced the user to
pick an abstraction level (pipeline vs asset) before understanding the
difference. The product's core promise — simpler than managed orchestrators — was at
risk. The recurring "we keep reworking this" pain traces to never having
fixed which model is primary.

Investigation of the existing code showed the unification is already ~80%
real in the implementation, not aspirational:
- A task that declares `outlets=[Asset(...)]` already *is* an asset
  producer; task↔asset is one entity in the data model, joined by outlets.
- Backfill is a single code path: `_resolve_target` resolves both
  `target.type='pipeline'` and `target.type='asset'` (the latter via
  `_find_producers_for_asset`) down to the same pipeline + `bulk_backfill`
  SFN. Asset backfill already collapses into pipeline backfill internally.
- The operational layer (dependency_wrapper SFN, run_task helper,
  step-actions, rerun/from-to, deps-signaling, PagerDuty) operates on a
  unit of execution and is model-agnostic.

So the divergence the team felt is in **UX/terminology**, not in the
runtime or DSL.

#### Decision

Fix the *primary lens*, not the engine.

1. **DSL is unchanged.** Authoring stays task-based: `@task` with
   `outlets` / `inlets` exactly as today. No `@asset` decorator, no
   sugar-alongside (that would be duplication — CLAUDE.md #2), no
   migration of existing pipelines. A task remains the technical unit of
   execution; a task with an outlet *is* its asset.

2. **The product accent is the asset.** UX/DX for operating the system —
   lineage, freshness, partitions, and backfill — is framed in assets
   (the data), not tasks (the steps). The asset is the lens through which
   the user views state and triggers work; tasks surface only when
   debugging a specific execution. Tasks without an outlet (sensors,
   cleanup, alerts) simply don't appear in the asset lens — they still run.

3. **Runtime/operational layer is unchanged.** Wrapper, run_task helper,
   step-actions, rerun, `_resolve_target`, `bulk_backfill` — all reused
   as-is. This is the stable foundation.

#### What changes vs what stays

| Area | Decision |
|---|---|
| Python DSL (`@task`, outlets, inlets) | **Unchanged** |
| task ≡ asset (via outlets) | **Unchanged** — already true |
| Pipeline as a grouping | **Unchanged** |
| dependency_wrapper / run_task SFN | **Unchanged** |
| step-actions / rerun / deps-signaling | **Unchanged** |
| `_resolve_target` (asset→pipeline) | **Reused** |
| UI tabs (Pipelines / Assets) | **Stay** |
| Backfill engine (`bulk_backfill`) | **Extended**, not replaced |
| Partition tracking tables in DDB | **Not needed** — current structure suffices |
| Backfill UX (asset-centric, partitions) | **Refined** — the product focus |
| `cascade auto/all/none` | **Renamed** → `downstream: off/dependents/all` |
| upstream awareness in backfill | **New** (the only genuinely new behavior) |

#### Scope of the only new behavior

Upstream smart-fill: backfilling an asset for date D resolves which
upstream assets are missing for D and builds them first, reusing existing
partitions where present. It reuses the existing DAG dependency graph and
the existing skip-completed partition check — wiring, not a new subsystem.
Paired with a symmetric `downstream` axis (renamed `cascade`). This is the
forward-development surface; everything else is fixed.

#### Consequences

- The "should I rework the model" question is closed. Future work develops
  the asset-centric UX and the upstream/downstream backfill axes; it does
  not revisit whether task or asset is primary.
- No DSL churn, no breaking change for existing pipelines, no parallel
  authoring path (CLAUDE.md #2, #9, #12 — reuse and no duplication).
- Detailed API/UI/SFN design for upstream smart-fill is deferred to its
  own ADR when that work starts; this ADR fixes only the concept.

#### CLAUDE.md alignment

#2 no duplication (one authoring path, one backfill engine), #6 alignment
before implementation (this ADR is that alignment), #9 no fix-on-fix
(consolidation, not patching), #12 maximize reuse (wire existing DAG /
partition-check / `_resolve_target` / wrapper rather than build new).
