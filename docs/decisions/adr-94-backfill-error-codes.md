---
area: sdk
accepted: by 2026-05-31
---
# ADR #94 — BackfillUpstream + backfill error-code registry onto the generated SSoT

#### Context

The upstream-lineage epic (v0.81–v0.84) added a `BackfillUpstream` mode
(`off`/`smart`/`force`) as the upstream mirror of `BackfillCascade`, but
hand-maintained it in two places — the backend validator's `_UPSTREAM_MODES`
tuple and a TS `type BackfillUpstream` union — outside the codegen enum SSoT
(ADR #72/#83/#93). `BackfillCascade`, its sibling, *is* canonical in
`polyris/constants.py` and generated; `BackfillUpstream` was a 9th, un-gated
family. Correct at the time, but exactly the silent-drift surface the enum SSoT
exists to remove.

Separately, `ui/src/utils/backfillErrors.ts` (the error-code → friendly
title+hint map) had drifted from the route's emitted codes: it was missing the
v83 `invalid_downstream*` rename codes and the v81 `invalid_upstream*` /
`upstream_cycle` codes (so both flagship features fell back to raw backend
messages), while still listing dead `invalid_cascade*` / `backfill_not_found` /
`range_outside_target` / `partition_start_violation`. Worse, its "parity" test
asserted a hardcoded `criticalCodes` array that was itself stale (listed the
dead `backfill_not_found`, none of the new codes) and therefore stayed green —
false confidence. This was the one cross-language backfill surface with no
generator gate.

#### Decision

Two different fixes because the two surfaces differ in kind.

1. **BackfillUpstream — eliminate the duplication (per ADR #93).** A canonical
   `class BackfillUpstream` lives in `polyris/constants.py`; `sync_enums.py`
   emits it into every `constants_generated.py` and into
   `ui/src/generated/enums.ts`. The backend `_UPSTREAM_MODES` is now built from
   `BackfillUpstream.{OFF,SMART,FORCE}` (re-exported via
   `console_api/constants.py`), and the TS `BackfillUpstream` type is
   re-exported from the generated module — no hand-maintained copy survives.
   `check-generate-enums` gates it for free; no new test.

2. **Error-code registry — gate the irreducible copy.** Unlike a status enum,
   the code → friendly-text map is hand-authored content (titles and hints);
   the map itself cannot be generated away. What *can* be single-sourced is the
   **set of codes** the map must cover. So a canonical `BACKFILL_ERROR_CODES`
   frozenset is added to `polyris/constants.py` and generated into `enums.ts`,
   and gated on both sides:
   - Backend `test_backfill_error_registry` pins the registry to the route's
     emitted `{'error': '<code>'}` literals in both directions (a new emitted
     code not in the registry fails; a registry code the route no longer emits
     fails).
   - UI `backfillErrors.test.ts` asserts the map covers every code in the
     generated registry — replacing the stale `criticalCodes` list.
   The map is filled for all 36 codes (operational ones like `internal_error` /
   `throttled` get explicit, deliberately-generic entries rather than falling
   through to raw backend text) and the 5 dead codes are removed.

   Scope: the registry covers **error** codes (the `{'error': ...}` shape)
   only. Warning codes (the `{'code': ...}` shape in the `warnings` array —
   `deprecated_field`, `cron_ambiguous`, `lineage`, `upstream`,
   `large_backfill`) are a separate surface and are not in this registry.

#### Consequences

- The upstream enum has one generated, gated source; the hand copies are gone.
- The backfill error-map can no longer silently drift: adding a backend error
  code fails the backend parity test until it is added to the registry, and the
  UI coverage test until it is given friendly text. The flagship downstream/
  upstream validation errors now render with titles+hints instead of raw JSON.
- Cost vs ADR #93: #93 could delete the duplicated value object because it was
  generatable; here the friendly copy is irreducibly human, so we keep a
  hand-maintained registry and gate it, rather than institutionalize a second
  hand-maintained *map*. Two test surfaces, but both check one generated list.
