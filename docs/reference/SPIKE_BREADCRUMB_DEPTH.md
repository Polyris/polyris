# Spike: Breadcrumb depth — is the current Pipelines-only drill-down the right shape?

**Question.** History (and Tasks, and the EE-only Assets/Backfills) show a flat,
single-segment breadcrumb ("History") no matter what you click on that page, while
Pipelines can extend to three levels ("Pipelines > branching-demo > Run be-9c5d..."). Is
this a bug, or is there a real recommendation to make here?

**Bottom line.** Confirmed not a bug — it's a direct, correct consequence of the current
navigation architecture: `/` explicitly redirects to `/pipelines/` (`app/page.tsx`'s own
docstring: "Root page — redirects to `/pipelines/` on first mount"), and every other
section's row-click handler (`AllRunsView`'s and `AllTasksView`'s `onPipelineClick`, both
wired to `navigateToExecution` in `App.tsx`) immediately navigates **away** to
`/pipelines/` rather than opening an in-place detail view. Once there, `mainView` becomes
`'pipelines'` and the three-level trail is correct. History never has an in-page state to
add a second crumb for, because nothing in History stays in History past a click.

**One real bug found and fixed while checking this:** `backfills` was missing from
`SECTION_LABEL` (`Header.tsx`) entirely — pipelines/tasks/runs/assets all had a properly
cased label, but backfills would have fallen through to the raw pathname-derived string
"backfills" instead. EE-only relevant today (`BackfillsView` is gated, unreachable in
this OSS build), but the same bug the moment that gate opens. Fixed alongside every other
section's label, with a regression test.

## Is the flat-vs-deep split itself worth changing?

Three shapes were considered, evaluated against "does the breadcrumb tell you the truth
about where you are and how you got here":

**A — Leave it as-is.** History/Tasks (and Assets/Backfills once reachable) are
deliberately thin: filterable finder lists whose only job is helping you locate a specific
run to jump into, not a place with its own identity worth a deeper trail. This is a common,
legitimate pattern — a search/filter hub feeding a canonical detail view, rather than every
section growing its own parallel detail view. Zero code changes; the "inconsistency"
dissolves once you know Pipelines is not a peer section but the app's one destination.

**B — Give History a second crumb for its own tabs ("History > Runs" / "History > Tsks").**
Would make the currently-active tab (Runs vs Tasks — both live under the "History" label
today) explicit in the breadcrumb, not just via the tab strip already visible on the page.
Low effort (the `mainView` already distinguishes `'runs'` from `'tasks'`; SECTION_LABEL
would just need a second, tab-specific label). Real question before doing this: is the tab
strip, which is already on-screen and already shows which tab is active, actually
insufficient on its own? Breadcrumbs and tabs both answering "which view am I in" is
redundant unless the tab strip is easy to miss (it isn't — it's directly under the section
label, in the same visual block screenshots show).

**C — Preserve entry section on drill-through, so the trail reads "History > orders-report
> Run ..." instead of "Pipelines > orders-report > Run ...".** This is the one that would
actually change today's *experience*, not just its cosmetics: right now, clicking a row in
History and clicking a row in the Pipelines sidebar land you in the exact same state
(`mainView='pipelines'`, that pipeline selected) — the breadcrumb can't tell you came from
History, because nothing about the app's state remembers that. To build this: `mainView`
would need to stop being the single source of the first crumb; something like an "entry
section" would need to travel alongside `selectedPipeline`/`selectedExecution` (set by
`navigateToExecution` at the moment of the jump, cleared by `goSection`/sidebar clicks).
Clicking that first crumb would then need to navigate back to *where you came from*
(`/runs/`, `/tasks/`), not unconditionally to `/pipelines/` — `goSection`'s current
`push('/${mainView}/')` already assumes `mainView` is truthfully "home," which stops being
true under this option. This is the one with a real UX payoff (a working "back to where I
was browsing" breadcrumb, not just a label) but it's the one that touches the shared
navigation state (`useAppStore`, `useStoreInit.ts`'s `navigateToExecution`, `Header.tsx`'s
`goSection`) — every call site that currently assumes "the first crumb always means go to
mainView-root" would need re-checking.

## Not resolved by this spike

Whether B or C (or neither, i.e. A) is worth building is a product call, not a technical
one — B is cheap and cosmetic, C is a genuine behavior change with a real payoff but a
wider blast radius across the navigation state that's shared by every section, not just
History. No code changes proposed here beyond the confirmed `backfills` label bug, which
was independent of this question either way.
