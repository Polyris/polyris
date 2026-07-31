# ADR #99 — UI open-core exclusion (generated active module)

> **Status:** Accepted — mechanism proven by spike on the project's own stack
> (Next.js 16.2 static export, Turbopack default): full build (ee present) and
> OSS build (ee removed) both compile, `tsc --noEmit` clean in both, Team
> surface present in full / absent in OSS. The physical carve follows in stages,
> mirroring the backend split (ADR #98).
>
> **Amended by ADR #100:** the UI proprietary tree is now organised into tier
> packages — `ui/src/ee/team/` and `ui/src/ee/enterprise/` — and the generator
> scans `src/ee/*/index.ts`, merging the `surface` each present tier exports
> (instead of a single `src/ee/index.ts`). free↔paid stays the physical strip
> described below; team↔enterprise is a runtime entitlement (`can()`, not a
> strip), so Enterprise components ship inside the paid bundle and are gated at
> render. The flat carve below completed first; this is its tiered refactor.

## Context

ADR #98 made the open-core boundary **physical**: proprietary code lives under a
distinct root, and "what is in the build" — `ee/` present or stripped — decides
the tier. No runtime flag. The backend (`console_api/ee/`) and SDK
(`polyris/_ee/`) already work this way.

The UI (`ui/`) is the last piece and the hardest. Facts that shape the decision:

- It is a **Next.js 16.2 static export** (`output: 'export'`, S3 + CloudFront).
  Next 16 uses **Turbopack by default** for `next dev` *and* `next build`; a
  custom `webpack` hook is ignored unless `--webpack` is passed.
- The tier surface is **mixed**, like the backend's mixed route modules.
  `utils/api.ts` is only a thin verb wrapper (`get/post/put/delete(path)`); the
  `/api/...` path strings are scattered across hooks and components. There is no
  central endpoint registry and no pre-existing edition/feature seam.
- A static export bundles the reachable import graph. To keep Team code out of
  the OSS bundle, the free build must compile with the proprietary modules
  **physically absent**, and `tsc` must stay green without them.

A runtime tier flag (`window.CONFIG.TIER` hiding Team UI) was considered and
**rejected**: it leaves proprietary source in the public bundle (defeats source
separation), adds a permanent gate to every Team feature, and diverges from the
backend mechanism (violates "one way", Principles #1/#12). The agreed release
sequence ships the free console *with* the OSS backend (v0.3), so no
full-UI-against-OSS-backend configuration is ever shipped — the runtime flag
would be a band-aid for a problem the product does not have.

## Decision

Exclude Team UI the same way the backend excludes Team routes — **physically, at
build time** — using a **generated active module** consumed by ordinary path
resolution (no bundler alias):

- **One existence check** (a small prebuild generator) writes
  `ui/src/ee-active.generated.ts`:
  - `ee/ui/index.ts` present (full build) → `export * from '<relative>/ee/ui';`
  - absent (OSS build) → an empty stub that satisfies the type contract.
- **Free code imports the generated module** via the standard `@/*` mapping
  (`import { … } from '@/ee-active.generated'`). It never references an `ee/ui`
  path directly.
- A free, always-present **type contract** (`ui/src/ee-contract.ts`) defines the
  shapes the boundary exposes (e.g. the Team-feature/slot registration types).
  The stub is typed against it, so **`tsc` is decoupled from the physical
  presence of `ee/ui`** — typecheck is green whether or not Team code is on disk.
- **OSS build = remove `ee/`** (one rule, exactly like the backend). The
  generator sees no `ee/ui`, emits the stub, the free build compiles, Team
  surface is gone. No code edits, no runtime flag.

This is bundler-agnostic: it relies only on plain module resolution, not on any
Turbopack/webpack alias internals (which change across releases). It also matches
the repo's existing codegen idiom (the enum codegen already generates committed
TypeScript consumed by the app).

### Tier boundary (UI)

Derived from the backend route split (ADR #98): a Team route ⇒ its UI is Team.

- **Free (stay in `ui/src`):** pipeline / run / task **reads**, DAG graph,
  `list_assets`, health, pipeline run + register, auth/login, layout/nav, error
  boundary, shared primitives.
- **Team (move to `ui/src/ee/`):** `ApiTokensSection`; asset
  matrix / lineage / drift / glue / queue UI (`AssetMatrixView`,
  `AssetLineageFlow`, asset-tabs `GlueSyncPanel` / `TabLineage` / `TabEvents` /
  `TabPartitions` / `TabSchema` / `TabChecks`, `SchemaCopyButtons`); all
  backfill UI (`BackfillModal` / `BackfillDetailPage` / `BackfillsListPage`,
  `GanttChart`, `CalendarView`); task/execution **action** controls
  (`TaskDetailModal` intervention bits).
- **Mixed hooks — split at function level** (free part stays, Team part →
  `ee/`), exactly like the backend's mixed route modules:
  `usePipelineActions` (Team mutations), `usePipelineQueries`,
  `useAssetQueries`, `useGlobalQueries`, `useBackfillQueries`,
  `usePipelineData` (pipeline metrics/logs).
- `CommandPalette` placement is an open judgment call (resolved during the carve).
- `HelpModal` only documents endpoints as static text (no live calls) → stays free.

### Invariant and slot contract

- Free code **never** imports from `ui/src/ee/`; `ui/src/ee/` **may** import free
  modules (shared helpers, primitives, the contract). Same direction as the
  backend (`ee/team` imports `routes/`, never the reverse).
- Free host components render Team pieces through the active-module barrel
  (slot lookup / registration), so a free build with the stub renders the free
  surface and simply omits Team slots.

## Alternatives considered

- **Turbopack `resolveAlias` `@ee` → real-or-stub.** Proven by the same spike and
  also correct. Rejected as the default because it couples the seam to Turbopack
  alias behaviour (absolute paths already fail; relative-only is an undocumented
  quirk; Turbopack ships hundreds of changes per release). The generated module
  achieves the same exclusion with zero bundler-internal dependence.
- **Runtime tier flag** (`window.CONFIG.TIER`). Rejected — see Context.
- **Separate published npm package for Team UI.** More packaging ceremony than an
  in-repo `ee/ui` for a single static-export app; deferred (same posture as the
  SDK's PyPI path, ADR #98 / BACKLOG).
- **Two separate app builds / webpack-forced build.** DRY violation / swims
  against the Next 16 Turbopack default.

## Consequences

- The OSS UI builds with `ee/` absent via the prebuild generator; one generated
  file (`.gitignore`d, with a committed stub fallback for editors/first build)
  plus a ~10-line generator script.
- The carve is **staged** like the backend: establish the boundary
  (contract + generator + `ee/ui` barrel + slot wiring), then move clean Team
  components, then function-split the mixed hooks, keeping the tree green at each
  step.
- CI gains a guard mirroring the backend's OSS-strip check: with `ee/` removed,
  `next build` must succeed, `tsc --noEmit` must pass, and the built output must
  contain **no** Team markers.
- Version stays behaviour-preserving for the full build (the full bundle is
  byte-for-byte the same surface; only the build-time seam is added).
