---
area: open-core
accepted: by 2026-08-17
---
# ADR #105 — Asset console gated to Team; the asset engine stays free

**Context.** The open-core boundary (ADR #99/#104) is "backfill/ops/governance =
paid; engine = free." Assets sat inconsistently across the seam: the asset
*engine* (SDK asset declarations, partition lambdas, asset tables) is free and
pipelines depend on it, but the asset *console* — the `/assets` view (matrix,
lineage, detail, asset-tabs) plus the single free read route `GET /api/assets` —
was split awkwardly. The OSS UI gated the whole `AssetsView` as a Team slot
(showing the Team-tier fallback) while the OSS backend still served
`GET /api/assets` and the README advertised the asset matrix as free. Net: a
nav tab that led to a "not available" page, a backend route nothing in OSS
called, and docs that disagreed with the build.

**Decision.** The asset **console** stays in Team for now, but instead of reading
as a permanent paid feature it is surfaced in OSS as *coming soon to open-core* —
it is on the graduation roadmap and will return to free feature-by-feature. The
asset **engine** stays free throughout.
1. **Frontend.** The `Assets` nav tab stays **visible** in OSS (not hidden). A
   visit to `/assets` renders a `ComingSoon` placeholder ("Asset console is
   coming in an upcoming release"); the real `AssetsView` lives in `ee/team/` and
   renders only in paid builds. The copy is deliberately **tier-agnostic** (no
   "open-core" wording) so the same notice is reusable in paid builds too.
   `ComingSoon` and `EeFeatureFallback` both render a shared, presentational
   `EmptyState` primitive (icon + title + optional description + action slot;
   styled via `.empty-state` design tokens, so it themes light/dark). They differ
   only in copy and icon: `EeFeatureFallback` marks a permanently Team-tier view
   ("not available in this edition", lock icon), `ComingSoon` signals a
   not-yet-shipped capability ("coming in an upcoming release", sparkles icon).
   (Other paid view-modes — Gantt, calendar — keep the `EeFeatureFallback`
   Team-tier message.)
2. **Backend.** The free `GET /api/assets` route moved out of OSS:
   `routes/assets.py` → `ee/team/assets_list.py` (added to `team.MODULES`), and
   `assets` was dropped from `main.py`'s `ROUTE_MODULES`. The shared
   `_build_assets_from_pipelines` helper moved with it; `ee/team/assets.py`,
   `ee/team/matrix.py`, and their tests now import it from `ee.team.assets_list`.
3. **DAL stays free.** `dal/assets_repo.py` (and its `dal/__init__` re-exports)
   remain in the free tree because the Team asset routes import them via `dal`;
   in the OSS build they are present but unused (harmless), avoiding a `dal`
   restructure.
4. **Engine untouched.** The SDK's asset modules and the SAM asset tables /
   lambdas stay free — OSS pipelines can still produce and wait on assets.
5. **Docs.** README presents the asset console (matrix + lineage) as coming to
   open-core in an upcoming release; the engine is described as already free.

**Reversibility.** This is the РОЗЧЕПЛЕННЯ/overlay model working as intended:
re-freeing an individual asset feature later is a `git mv` from `ee/` into the
free tree (plus a `ROUTE_MODULES`/nav line), not a rewrite (ADR #99).
