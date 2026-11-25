---
area: ui
accepted: by 2026-05-08
---
# ADR #48 — AssetDetailPage Composition — Tabs as Independent Sub-Components

**Status:** Adopted in v0.75.8.

**Context:** Through v0.75.0–v0.75.7 the asset-detail view grew from a
modest component into a 1124-line monolith. All six tabs (Overview,
Schema, Partitions, Events, Checks, Lineage), the Glue drift panel,
the schema-copy actions, and the sidebar were inlined in
`AssetDetailPage.tsx`. The growth path was straightforward — each
feature (typed schemas, drift detection, copy buttons, conflict
banners, status banner refinements) added 50–200 lines to the same
file. By v0.75.7 the file was hitting the readability limit:

  - Any change required scrolling through ~1000 lines of unrelated tab
    code to find the right block.
  - Tab-internal helpers (e.g. `humanizeGlueError`, `renderMarkdown`,
    `DriftSection`) sat at the bottom of the file mixed with the
    component, blurring the line between page-level and tab-level.
  - Tests for the page exercised all tabs through one mounted component,
    making test failure messages ambiguous about which tab regressed.
  - The next feature on the backlog (interactive lineage editor, asset
    checks UI authoring) would push the file past 1500 lines.

**Decision:** Split the page into an orchestrator and one component per
tab, organised under `ui/src/components/asset-tabs/`:

```
ui/src/components/
├── AssetDetailPage.tsx        ← orchestrator (~370 lines):
│                                   header, tab nav, body dispatcher,
│                                   sidebar, page-scoped queries
└── asset-tabs/
    ├── types.ts               ← TabContext, AssetDerived
    ├── index.ts               ← barrel re-export
    ├── TabOverview.tsx        ← (~245 lines)
    ├── TabSchema.tsx          ← (~175 lines)
    ├── TabPartitions.tsx      ← (~125 lines)
    ├── TabEvents.tsx          ← (~85 lines)
    ├── TabChecks.tsx          ← (~30 lines, placeholder)
    ├── TabLineage.tsx         ← (~40 lines, wraps AssetLineageFlow)
    ├── GlueSyncPanel.tsx      ← (~205 lines, used by TabSchema)
    ├── SchemaCopyButtons.tsx  ← (~110 lines, used by TabSchema)
    └── glueHelpers.tsx        ← humanizeGlueError + DriftSection
```

**State allocation rules** (the heart of why the split works):

  - **Page-scoped state stays in the orchestrator.** This is anything
    that needs to outlive a tab switch:
    - `activeTab` — which tab is shown
    - `useAssetEventsQuery` — fetched once, consumed by 3 tabs
    - `useAssetGlueSchemaQuery` — re-mounting TabSchema must not
      re-trigger the Glue API call
    - `derived` (memoized AssetDerived) — same source of truth across tabs
  - **Tab-local state stays inside the tab.** This is interaction state
    that resets if the user navigates away:
    - `selectedPartition` — internal to TabPartitions
    - `selectedEvent` — internal to TabEvents
    - `copied` — internal to SchemaCopyButtons (UI affordance only)

The rule is simple: if losing the state on tab-switch would surprise
the user, hoist it to the orchestrator. If "I clicked back into this
tab and my selection is gone" is acceptable UX, the state belongs in
the tab.

**Props contract — `TabContext`:**

Every tab receives a `TabContext` (defined in `asset-tabs/types.ts`)
containing the seven fields used by 2+ tabs. Tab-specific extras
extend the context on the tab's own Props interface (e.g.
`TabSchemaProps extends TabContext` adds `derived`, `schemaConflicts`,
`glueQuery`). This pressure keeps the shared context narrow — when a
field is needed by exactly one tab, putting it on `TabContext` is the
wrong move because every other tab now has to accept a useless prop.

**Why not React Context for shared data?** Context would let tabs read
state without explicit props. Rejected:

  - Hides the dependency graph — you can't tell what data a tab uses
    by reading its signature; you have to read its body
  - Harder to test — every tab test needs a Context provider wrapper
  - Encourages "since we have it in context, we'll just use it from
    anywhere" creep, which breaks the page-vs-tab state rules above

Explicit props are the right tradeoff at this scale. If the page
grows to dozens of tabs and the prop list becomes unwieldy, revisit —
but that's a problem we'd be lucky to have.

**Why not lazy-load tabs?** Each tab is small. Code-splitting at the
tab boundary would save bundle size only if tabs were heavy (e.g.
embedded charts, large libraries). They aren't — biggest tab
(TabSchema) is ~175 lines and pulls only icons + utils. Deferred
until measurable bundle pressure exists.

**Adding a new tab:** the procedure is one file create + three small
edits. Documented in `docs/development/ADDING_ASSET_TABS.md` with a
working template.

**Refactor triggers — when to apply this pattern elsewhere:**

  - A component file exceeds ~800 lines AND mixes multiple distinct
    UI surfaces (not just lots of small helpers — distinct surfaces
    is the trigger). The page's natural "tabs", "sections", or "modes"
    are the seam.
  - A tab/section's internal helpers (counter, modal, formatter)
    start to outnumber the tab's own JSX. Extract them as siblings
    in a `components/` subdirectory.

The pattern is mechanical: identify the seams, create a directory,
move the chunks, pass shared state down as props. There is no
abstraction tax — each tab is just a function that takes props and
returns JSX, the same as before.
