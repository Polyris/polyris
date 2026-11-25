# ADR #80 — Backfill modal viewport overflow + dead CSS cleanup (v0.79.8)

#### Context

The backfill modal (one `BackfillModal` component, reused for pipeline /
asset / cell / task contexts) overflowed the viewport when the user
expanded Options and the "Tasks to run" list. The footer action buttons
(Cancel / Preview / Start Backfill) were pushed off-screen with no way to
reach them — the modal grew unbounded and was vertically centered, so
both top and bottom clipped.

Root cause: a **class mismatch**. The live modal used
`className="modal bf-modal"`, but `.bf-modal` had only
`max-width: 600px` — no height cap. The correct height-cap CSS
(`max-height: 90vh` + flex column + scrollable body) existed under
`.bf-backfill-modal`, a class **no component used** — dead CSS left over
from the pre-v0.78 modal that was renamed to `bf-modal` without moving
the rules.

#### Decision

**Fix (A): height-cap the live `.bf-modal`.**

```css
.bf-modal {
    max-width: 600px;
    width: 100%;
    max-height: 90vh;                  /* fallback */
    max-height: calc(100dvh - 2rem);   /* precise; dvh handles mobile chrome */
    display: flex;
    flex-direction: column;
}
.bf-modal .modal-body { overflow-y: auto; flex: 1 1 auto; min-height: 0; }
.bf-modal .bm-modal-header,
.bf-modal .modal-footer { flex-shrink: 0; }
```

Header and footer pin; only the body scrolls. The component already had
the right JSX structure (`ModalHeader` / `ModalBody` / `ModalFooter` as
direct children) — the CSS just wasn't reaching it.

**Fix (B): responsive task list.**

```css
.bf-task-list { max-height: min(180px, 24vh); overflow-y: auto; }
```

`min()` shrinks the inner list on short screens (24vh ≈ 144px on a
600px-tall laptop) so it never dominates; on tall monitors it caps at
180px. The modal body's own scroll handles overflow beyond that.

**Responsive (all screen sizes):** global overlay gains
`padding: 1rem; box-sizing: border-box;` so no modal touches the viewport
edge on small screens. The `.bf-modal` max-height `calc(100dvh - 2rem)`
exactly matches that padding (1rem top + 1rem bottom), so the modal fills
the available space precisely without overflow on any height. `dvh`
(dynamic viewport height) means a mobile browser's collapsing URL bar
doesn't push the footer out of reach.

**Dead CSS removed (CLAUDE.md #1):** the audit that found the mismatch
also found the entire old-modal stylesheet was dead — every
`.bf-backfill-*` class (info, pipeline-name, dates, selection, group,
item, summary, auto-vars, advanced, variables, disclaimer) and
`.bf-nav-pills--plain`. None referenced by any `.tsx`. The CSS comment
literally said "Replaces .bf-* after old BackfillModal removed (Phase
5c)" — the old modal was gone, the CSS wasn't. Removed 220 lines across
two interleaved blocks (the live `.bf-option-*` / `.bf-task-*` rules
between them were preserved).

#### Why this overlay change is safe for all modals

The overlay (`.bm-modal-overlay`) is shared by every modal. Adding 1rem
padding shrinks available space by 2rem total. Any modal with
`max-height: 90vh` still fits within `100vh - 2rem` whenever the viewport
exceeds 320px tall (2rem < 10vh) — always true in practice. Short modals
are unaffected (they're nowhere near the cap). The change is a strict
improvement: every modal gets edge breathing room on small screens.

#### Guard tests

Two structural guards in `BackfillModal.test.tsx` (the test renders the
real `BaseModal`, not a stub):
- modal container carries the `bf-modal` class (catches the exact rename
  that caused this bug),
- `.modal-body` and `.modal-footer` both present under the dialog (the
  two elements the height-cap CSS depends on).

These can't verify scroll/layout in jsdom (no real layout engine), but
they lock the class/structure contract the CSS relies on. A future rename
of `bf-modal` or removal of the body/footer wrappers fails the test
immediately.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| vitest | 833 | **835** | +2 (layout contract guards) |
| Python total | 1358 | 1358 | 0 |
| tsc strict | 0 errors | **0 errors** | — |
| CSS lines | — | **−220 dead** | cleanup |
| **All gates** | green | **green** | — |

#### Lesson

The bug looked like "modal too tall" but was really "CSS pointed at a
class that no longer existed." The fix existed in the codebase the whole
time — orphaned under a dead class name. Two lessons:

1. **Renames must move the rules, not just the markup.** When
   `bf-backfill-modal` became `bf-modal`, the CSS should have moved with
   it. It didn't, and the height cap silently stopped applying.
2. **Dead CSS hides working CSS.** The dead `.bf-backfill-modal` block
   looked like it was handling the modal — anyone scanning the file would
   assume the height cap was active. Removing dead code isn't just
   tidiness; it removes false signals about what the live code does. A
   CI check for unreferenced BEM-prefixed classes would have caught both
   the dead block and the mismatch.
