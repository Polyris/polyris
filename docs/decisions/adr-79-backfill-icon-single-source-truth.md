# ADR #79 — Backfill icon single source of truth (v0.79.7)

#### Context

The backfill affordance appeared across the console with three different
icons, chosen ad-hoc per component:

| Surface | Icon before |
|---|---|
| Nav "Backfills" tab (Header) | Rocket |
| Backfills list page header | Rocket |
| Backfill modal header (cell/asset) | Rocket |
| Backfill detail page header | Rocket |
| Backfill badge in All Runs | Rocket |
| Backfill notification (item + toast) | Rocket |
| "Backfill" button on pipeline DAG | Rewind |
| Help legend backfill entries (×2) | History |

Even the centralized `utils/icons.tsx` carried the inconsistency:
`ActionIcons.backfill = Rewind` but `ContextIcons.backfill = History`.
Ten call sites across eight files, three icons, no single owner.

Mike flagged it from the UI: the nav tab (rocket) and the DAG button
(rewind `«`) didn't match, and the modals used a third style. He chose
**Rewind** as the canonical backfill icon.

#### Decision

**1. One definition.** `ActionIcons.backfill` (in `utils/icons.tsx`) is
THE backfill icon. Set to `Rewind` per Mike's choice. A comment marks
it as canonical: every backfill affordance renders this; change it here
and it changes everywhere.

**2. ContextIcons aliases ActionIcons.** `ContextIcons.backfill` no
longer hardcodes a lucide component — it references
`ActionIcons.backfill`:

```ts
export const ContextIcons = {
    ...
    backfill: ActionIcons.backfill,  // single source, not History
    ...
};
```

This removes the second, divergent definition. ActionIcons is declared
before ContextIcons in the module, so the reference resolves cleanly.

**3. All 10 sites consume the indirection.** No component imports a raw
`Rocket` / `Rewind` / `History` for backfill anymore. They use
`ActionIcons.backfill` (or `ContextIcons.backfill` in the help legend,
which is semantically a "context section" icon and aliases the same
component). Components that imported the icon directly from `lucide-react`
(e.g. `Header.tsx`) now import `ActionIcons` from `@/utils/icons`.

**4. Dead code removed.** After the migration, the `History` lucide icon
was no longer referenced anywhere (the `Clock`-based "History" text label
in TaskDetailModal is unrelated). Its import and re-export were removed
from `utils/icons.tsx` per the no-dead-code principle.

#### What did NOT change

- **`Rocket` stays** for its real meaning: `ActionIcons.run`,
  `ElementIcons.dagTrigger`, the Run button, run notifications, the
  run-action help legend entry, and DAG-trigger nodes in the lineage
  flow. Rocket = "launch / run", which is the opposite of backfill —
  conflating them was the original bug.
- **`Rewind` is now backfill-only**, owned by `ActionIcons.backfill`.

#### Guard test

`utils/icons.test.tsx` (3 tests) locks the invariant:
- `ActionIcons.backfill` is defined.
- `ContextIcons.backfill === ActionIcons.backfill` (same reference —
  proves the alias, catches re-divergence).
- `ActionIcons.backfill !== ActionIcons.run` (catches the most common
  pre-v0.79.7 mistake of collapsing backfill back into Rocket).

If a future edit hardcodes a different backfill icon in either map, the
`toBe` reference check fails immediately.

#### Test mock update

Component test mocks for `@/utils/icons` previously stubbed
`ActionIcons: () => null` (a function). Code now does
`ActionIcons.backfill`, so the mock had to be an object. Replaced with
`new Proxy({}, { get: () => () => null })` — any icon key resolves to a
null-rendering component, future-proof against new keys. Applied across
15 (`ActionIcons`) + 14 (`ContextIcons`) test files. HelpModal's mock
gained a `ContextIcons` entry it previously lacked.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| vitest | 830 | **833** | +3 (icon SSoT guard) |
| Python total | 1358 | 1358 | 0 |
| tsc strict | 0 errors | **0 errors** | — |
| **All gates** | green | **green** | — |

#### Lesson

"Make the icon consistent" looked like a find-and-replace, but the real
fix was structural: the inconsistency existed because there was no single
owner. Replacing ten hardcoded icons with ten hardcoded copies of the
same icon would have re-drifted the moment someone touched one file. The
durable fix is the indirection — `ActionIcons.backfill` as the one
definition, `ContextIcons.backfill` aliasing it, a reference-equality
guard test, and zero raw icon imports for backfill. Now "change the
backfill icon" is a one-line edit, which is what consistency actually
requires.

The same pattern applies to any icon used in 3+ places: route it through
the icon map, never import the lucide component directly in feature
components. (Existing direct imports elsewhere are pre-existing debt; this
ADR fixes backfill specifically and sets the precedent.)
