---
area: ui
accepted: 2026-05-27
---
# ADR #64 — Standard keyboard shortcut convention

#### Context

The `useKeyboardShortcuts` hook and `SHORTCUTS` catalog have existed
since early v0.x, but only two surfaces used them: global App routes
(`?`, `ctrl+k`, `Esc`, `ctrl+shift+t`) and the PipelineDetail refresh
(`ctrl+r`). Everything added since — Backfills list/detail, All Tasks,
All Runs, Asset matrix/detail, BackfillModal, TaskDetailModal — had
zero keyboard wiring, even where the visible buttons clearly invited it
(Refresh, tab switching, modal Submit).

The problem isn't the hook (which works well). The problem is that
adding a shortcut wasn't on anyone's checklist when shipping a new
view. Each new surface diverged from convention silently — and the
divergence compounded.

External UX feedback (Mike, 2026-05-27): *"шорткати у нас ніби є
підтримка лише для функціоналу який ми давно додавали, а для всього
нового в декількох табах і вкладках ми не додавали підтримки. Також це
треба в claude.md правило занести аби про це не забувалось."*

#### Decision

Codify a **standard shortcut mapping by surface type**, and add a
CLAUDE.md rule (#19) requiring it on every new view/page/modal/tab
container before merge.

| Surface type | Standard shortcuts |
|---|---|
| **Global** | `?` help, `⌘K` search, `Esc` close overlay, `⌘⇧T` theme |
| **List views** (BackfillsListPage, AllTasksView, AllRunsView, AssetMatrixView, …) | `⌘R` refresh, `/` focus filter, `J` next row, `K` prev row, `Enter` open selected |
| **Detail pages** (BackfillDetailPage, AssetDetailPage, PipelineDetail, …) | `⌘R` refresh, `Esc` back to list |
| **Multi-tab containers** (TaskDetailModal, AssetDetailPage tabs, PipelineDetail viewModes, HelpModal) | `1`, `2`, `3` … switch tab in declaration order |
| **Modals with primary action** (BackfillModal, …) | `Esc` close (via BaseModal), `⌘↵` (ctrl+enter) submit |

Reserved keys: `j`, `k`, `1`–`9`, `/`, `?`, single letters bind only
per the table above. Non-standard actions use modifier-key combos
(`ctrl+*`, `ctrl+shift+*`).

#### Catalog additions

`SHORTCUTS` extended with:

- `FOCUS_FILTER: '/'` — list-view filter focus
- `OPEN_SELECTED: 'enter'` — list-view open highlighted row
- `SUBMIT: 'ctrl+enter'` — modal primary action
- `TAB_1` … `TAB_9` — numeric tab switching

`NEXT_TASK` / `PREV_TASK` / `EXPAND` / `COLLAPSE` were already in the
catalog but had no consumers. They are now wired (`j`/`k`) in list
views.

#### Surfaces wired in this release

| Surface | Shortcuts added |
|---|---|
| BackfillsListPage | refresh, `/`, j/k, Enter |
| BackfillDetailPage | refresh, Esc back |
| AllTasksView | refresh, `/` |
| AllRunsView | refresh |
| AssetMatrixView | refresh |
| AssetDetailPage | refresh + tabs 1-6 |
| BackfillModal | ctrl+enter submit |
| TaskDetailModal | tabs 1/2/3 |
| PipelineDetail | tabs 1/2/3 for viewMode (already had refresh) |
| HelpModal | tabs 1/2/3/4 |

#### HelpModal sync

`KeyboardShortcutsTab` rewritten to group bindings by surface type
matching the convention above. Discoverability matters as much as
wiring — users finding the help modal need to see exactly what's
bound, not a stale subset.

#### Non-surfaces (not wired)

- Inline `onKeyDown` for `Enter`/`Space` on individual focusable
  elements (buttons, links, table cells, sidebar items) — these are
  accessibility, not shortcuts, and stay as-is per a11y standards.
- `useUrlSync` / browser back-forward — handled by routing, not the
  shortcut system.

#### Tests

New shortcut tests on the major surfaces:

- `BackfillsListPage.test.tsx`: ⌘R, /, j/k highlight, Enter opens (+4 tests)
- `BackfillDetailPage.test.tsx`: ⌘R, Esc (+2 tests)
- `TaskDetailModal.test.tsx`: 1/2/3 tab switching + closed-modal no-op (+3 tests)

Smaller surfaces tested implicitly via existing render tests (no
regressions on 752 → 761 vitest count).

#### Forward-looking

The CLAUDE.md rule means future additions get shortcuts at the same
time as the feature. If a future surface doesn't fit the table — say,
a graph visualization with pan/zoom that wants arrow keys — the rule
prompts the conversation: either extend the standard table here, or
explain in the PR why this surface is an exception.

### 64.1 Keyboard shortcut convention — revised key allocation (v0.78.5)

#### Context

ADR #64 (v0.78.3) introduced the standard mapping. Live UX revealed a
fatal conflict: numeric keys `1-9` were used by both **top-level
navigation in App.tsx** (`1`=Pipelines, `2`=Assets, `3`=Tasks, `4`=Runs,
`5`=Backfills) and **inner-surface tab switching** (PipelineDetail
viewMode, AssetDetailPage tabs, TaskDetailModal, HelpModal).

`document.addEventListener` registers a separate listener per component
that calls `useKeyboardShortcuts`. When the user presses `2` while on
the Pipelines page:

1. App.tsx listener fires → `router.push('/assets/')`
2. PipelineDetail listener fires → `setViewMode('gantt')`

Both happen. The user sees Gantt view briefly, then the route change
swaps the view, then they land on /assets/. Or vice-versa depending on
React reconciliation timing. Either way: not what the user expected.

External UX report (Mike, 2026-05-27): *"коли клацнув на 1 — на пайплайн
переключило (це ок), коли звідти клацаю 1,2,3 — переключає між DAG /
Gantt / Calendar (не ок, бо коли клікну 2 — я очікую переключитись на
табу assets)."*

#### Decision

Reserve **numeric keys `1-9` exclusively for top-level navigation** in
App.tsx. Inner-surface tab switching uses **letter keys matching the
first letter of the tab name** (or another short, memorable letter when
the first letter is taken).

Revised mapping:

| Surface | Old (v0.78.3, removed) | New (v0.78.5) |
|---|---|---|
| App.tsx top-level | `1`/`2`/`3`/`4`/`5` | `1`/`2`/`3`/`4`/`5` (unchanged) |
| PipelineDetail viewMode | `1`/`2`/`3` | `d` / `g` / `c` (DAG / Gantt / Calendar) |
| AssetDetailPage tabs | `1`–`6` | `o` / `s` / `p` / `e` / `c` / `l` (Overview / Schema / Partitions / Events / Checks / Lineage) |
| TaskDetailModal tabs | `1`/`2`/`3` | `d` / `t` / `a` (Details / Timeline / Actions) |
| HelpModal tabs | `1`/`2`/`3`/`4` | `s` / `i` / `b` / `a` (Shortcuts / Icons / Backfill / API) |

`SHORTCUTS.TAB_1` … `SHORTCUTS.TAB_9` are **removed from the catalog**
(no consumers; constants would only invite future re-introduction of
the same bug).

#### Cross-surface letter overlap

Two surfaces may both define a letter that maps to different actions
(`c` = Calendar in PipelineDetail and `c` = Checks in AssetDetailPage;
`a` = Actions in TaskDetailModal and `a` = API in HelpModal). This is
acceptable because:

- PipelineDetail and AssetDetailPage are **different routes**; user
  cannot be on both simultaneously.
- TaskDetailModal and HelpModal are both modals but **mutually exclusive**
  (only one modal open at a time in practice).

If a future modal is designed to overlay a page that uses the same
letter, the modal's handler will fire alongside the underlying page's.
Cleanest mitigation when this happens: gate the underlying page's
shortcut on a "no modal open" flag from the app store. Defer until
needed.

#### Forbidden bindings

- **Numeric `1`-`9` outside App.tsx**: forbidden. Reserved for top-level
  nav. Future PRs that bind numeric keys at any non-App surface must be
  rejected at review.
- **Single letters that are already SHORTCUTS catalog entries** (`j`,
  `k`, `e`, `c`): allowed at surfaces only when those catalog entries
  are not wired in the same context. `e` (catalog: EXPAND) is wired
  nowhere globally, so AssetDetailPage's `e` = Events is fine.

#### CLAUDE.md #19 update

Rule #19's "Multi-tab containers: `1`, `2`, `3` … switch tab in
declaration order" is **replaced** with: "Multi-tab containers within a
page: letter keys matching the first letter of each tab name. Numeric
keys reserved for global navigation." See CLAUDE.md for the canonical
current text.

#### Tests updated

All shortcut tests on affected surfaces switched from `fireEvent.keyDown
{ key: '2' }` patterns to letter equivalents. No behavior tests removed;
existing tests rewritten to match new bindings. HelpModal got one new
assertion that "Top-level navigation" group is present in the rendered
help text.

#### Lesson

The original ADR #64 was written before live UX with real users. The
numeric convention "felt right" in isolation but immediately broke once
combined with the pre-existing global numeric nav. The lesson: shortcut
conventions need to be checked against **the full set of already-wired
handlers**, not the empty set. CLAUDE.md #19 update adds an explicit
"check for conflicts with App.tsx global nav" step.
