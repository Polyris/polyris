# ADR #40 — Responsive Layout: Flex Chains, No Viewport Magic Numbers

**Decision:** UI layout uses flex/grid chains for height inheritance. Mobile responsiveness is mandatory across all views via `_mobile.css` overrides or per-component `@media` queries.

**Context:** Several layout bugs traced to viewport magic numbers (`calc(100vh - 180px)`, `calc(90vh - 130px)`) that broke when:
- Browser zoom changed
- Header height changed
- Heavy components (ReactFlow, Gantt) were rendered inside scroll containers with capped height
- Mobile breakpoints were missing for new views (e.g. `AssetDetailPage` had zero `adp-*` mobile rules)

The bug pattern: a component with `height: 100%` sits inside a parent with `max-height: calc(100vh - Npx)`, so the inner component renders at a wrong size whenever `N` doesn't match the actual header height.

**Decision:**

1. **No viewport magic numbers.** Forbidden patterns:
   - `calc(100vh - Npx)` for height
   - `calc(100vw - Npx)` for width
   - Inline Tailwind `h-[calc(100vh-Npx)]`

   Allowed: parent-relative `calc(100% + Npx)` (for offsets), `calc(100vw - 1rem)` for dropdown-fit-on-mobile.

2. **Use flex chains.** A heavy component (ReactFlow, Gantt, table) inherits height like this:
   ```css
   .parent { display: flex; flex-direction: column; flex: 1; min-height: 0; }
   .heavy-component { flex: 1; min-height: 0; height: 100%; min-height: 400px; /* baseline */ }
   ```
   The `min-height: 0` is critical — without it, flex children won't shrink.

3. **Mobile breakpoints.** Three tiers in `_mobile.css`:
   - `≤1024px` — tablet: compact sidebars, hide breadcrumbs
   - `≤768px` — mobile: hamburger, sidebar overlay, modals → 95vw
   - `≤480px` — small mobile: minimal font sizes, 90vw sidebar

4. **Per-component mobile rules.** Components using CSS Modules (scoped class names) cannot be styled from `_mobile.css`. They must include their own `@media (max-width: 768px)` block at the end of their `.module.css` file.

5. **Modals.** Either use `BaseModal` (inherits `_mobile.css` rules for `.base-modal-content`) or add explicit `@media` block in own CSS module.

6. **Definite viewport height at root.** The `.app` shell must use `height: 100vh; overflow: hidden` — never `min-height: 100vh`. Reason: descendants with `flex: 1` or `height: 100%` resolve to content size when parent has no definite height. With `min-height` alone, ReactFlow/Gantt collapse to ~100×100px because their parent chain bottoms out at content-defined height somewhere up the tree. The `<body>` keeps `min-height: 100vh` so non-app content (LoginPage, error pages) still scrolls naturally. `.main` needs `min-height: 0` so flex children can shrink properly.

7. **TSX `className` strings vs CSS Module imports.** When a TSX file uses a plain string className like `className="alf-foo"` (without `import styles from './Foo.module.css'`), the class must be defined in a **global** CSS file (e.g. `_assets.css`). Editing a same-name class inside a `.module.css` file is **dead code** — CSS Modules hash the class name, so the global string never matches. Before editing any `.module.css`, verify the corresponding TSX actually imports it as a module:
   ```bash
   grep -E "import.*from.*Foo\.module\.css" Foo.tsx
   # If no import — that .module.css is dead code, edit the global CSS instead
   ```

**v0.70.8 cleanup:**
- 3 viewport magic numbers eliminated:
  - `_assets.css` — `.av-catalog max-height`
  - `_modals.css` + `BackfillModal.module.css` — `.bf-backfill-modal .modal-body max-height`
  - `AssetsView.tsx` — lineage tab inline Tailwind
- `AssetDetailPage` extracted from `.av-catalog` scroll container; rendered directly in `.av-main-content` flex column
- 5 components got mobile rules: `AssetDetailPage` (`adp-*`), `BackfillModal`, `CommandPalette`, `CalendarView`, `AssetLineageFlow` baseline `min-height`
- Lineage container desktop baseline: `min-height: 400px` (was only mobile rule)

**v0.70.9 root cause fix:** After v0.70.8, AssetDetailPage was still clipped and Lineage graph still collapsed. Root cause: `.app { min-height: 100vh }` instead of `height: 100vh`. Changed to definite height + `overflow: hidden`. Added `min-height: 0` to `.main`. Removed conflicting `grid-template-columns: 330px 1fr` from `.av-assets-view` (was clashing with `.av-assets-layout: 200px 1fr` since both classes always applied to same `<div>`).

**v0.70.10 lesson — TSX className/CSS Module mismatch:** `AssetLineageFlow.tsx` uses `className="alf-lineage-flow-container h-full w-full"` — plain string, NOT imported from `.module.css`. The component does not import its own `AssetLineageFlow.module.css`. Therefore the v0.70.8 edits to `.lineage-flow-container` inside `AssetLineageFlow.module.css` were dead code — the class was never used. The actual styling lives in global `_assets.css` under `.alf-lineage-flow-container`. After moving the fix to the correct location, the full `AssetDetailPage` flex chain still required `min-height: 0` and `display: flex; flex-direction: column` added to `.adp-body`, `.adp-main`, `.adp-content--lineage`. `panel-section` (used by AllTasksView/AllRunsView) also needed `flex: 1; min-height: 0` because `<main>` is flex column.

**Verification on UI changes:**
- `grep -rn "calc(100v[hw]" ui/src/` should return zero new matches
- `grep -rn "min-height: 100vh" ui/src/styles/` — should ideally only appear on `body` or auth gate root, not on `.app`
- For each `<div className="some-class">` in TSX, verify whether `some-class` is from imported CSS Module (`styles['some-class']`) or global CSS (plain string) — the styling source must match
- New views must have mobile rules covering at least `≤768px` breakpoint
- **Visual test** at viewport widths: 320px, 768px, 1024px, 1920px before merge — vitest runs in jsdom which doesn't apply real CSS, so automated tests cannot catch CSS layout bugs
