# Spike: Viewing a pipeline's current deployed structure after `polyris-deploy`,
# without tying it to any past or future execution

**Status: DECIDED AND IMPLEMENTED.** Option A (explicit toggle) shipped — see
"Resolution" at the end of this document.

**Question.** After `polyris-deploy` changes a pipeline's task structure or types, the
pipeline detail view still shows the graph tied to the last real run — which reflects the
structure **as it was when that run happened**, not what was just deployed. Seeing the new
structure today requires triggering an actual execution. Is there a way to show "what's
deployed right now" independent of runs — and if not, what's the right way to add it?

**Bottom line.** The backend already has exactly this capability, unused by the main
pipeline view. `GET /api/pipeline-dag` has a documented three-tier fallback
(`sam/lambdas/console_api/routes/pipelines_info.py`):

1. **Per-execution snapshot** (`dag_snapshot::{execution_name}`) — if `pipeline_execution`
   is given. Frozen on purpose: it exists so an *old* run keeps showing the structure it
   actually ran with, even after later redeploys change it.
2. **Registry** (`pipelines_repo.get(pipeline_name)`, `dag_source: 'registry'`) — the
   *current* deployed structure, written on every execution (including the register-only
   run `polyris-deploy` triggers on every deploy — see `Save_DAG_Snapshot`'s neighbor state
   `Register_Pipeline`, `polyris/generators.py`). This is exactly "what's deployed right
   now," and it updates on every `polyris-deploy`, with no execution required.
3. **Inferred from execution rows** — last-resort fallback if neither exists.

Tier 2 is not dead code — `usePipelineTasksList` (`ui/src/hooks/queries/
usePipelineQueries.ts`, used by the backfill modal's task picker, ADR #61) already calls
`/pipeline-dag?name=...` with no `pipeline_execution` and gets it. The gap is narrower than
"build a new feature": the main pipeline view's data hook, `usePipelineDetailQuery`,
*always* passes `pipeline_execution` whenever any `execution` value is set (which the
calling component resolves to "the latest run" by default) — so it only ever exercises
tier 1. There's no code path today where the main view asks for tier 2 on purpose.

## Why this isn't just "always show tier 2 by default" instead

Tier 1 is the right default for viewing a **specific, already-happened run** — you're
inspecting history, and the run's actual structure at the time is more useful than "what's
deployed now" (e.g., a task that's since been renamed or removed shouldn't vanish from a
run you're investigating). The complaint is specifically about the **gap between deploying
and running again** — there's no way, *in that window*, to preview the new structure. Tier
1 staying the default for viewing a selected run is correct; the missing piece is a way to
deliberately ask for tier 2 instead, independent of whichever run happens to be "latest."

## Rendering the skeleton — what's untested

Tier 2's response has the same shape (`nodes`/`edges`) as tier 1/3, but nodes carry no
execution status (no `success`/`failed`/`running`, no duration, no timestamps) — because
nothing ran. `DAGGraphFlow`/`DAGTaskNode` were not checked in this spike for how they
render a node with no status at all; today every node they've ever received has come from
either a live execution or a past one, so an "always neutral, never colored" node is an
untested rendering path, not a confirmed-working one.

## Options

**A — Manual toggle, e.g. "Current structure" next to the execution picker.**
Explicit, discoverable, no ambiguity about which mode you're in. Calls the same hook with
`execution: null`. Needs a UI decision on where it lives (next to the date/execution
picker seems natural) and how status-less nodes render (see above) — probably a distinct
neutral style, not reusing the "not yet run / waiting" color if that already means
something specific elsewhere in the graph.

**B — Automatic: default to tier 2 when the selected/latest execution predates the most
recent deploy.**
Needs a way to compare "latest execution's timestamp" against "when was this last
deployed" — the registry item likely already has *a* timestamp from the last
`Register_Pipeline` write; whether it's precise enough and consistently updated across
every deploy path (bulk `--all`/`--only` included) wasn't checked here. Removes the need
for the user to notice and toggle anything, at the cost of the view silently changing
what "the graph" means depending on timing, which could be more confusing than a visible
toggle when the two structures genuinely differ (e.g. a renamed task) rather than just
"nothing to show yet."

**C — Both: automatic tier-2 fallback when there's no execution at all (already effectively
true, since tier 1 requires `pipeline_execution`), plus an explicit toggle for "I know
there's a recent run, but show me current anyway."**
Covers the brand-new-pipeline case for free and gives an explicit escape hatch for the
"just redeployed, haven't rerun yet" case without ever silently swapping what an existing
view means.

## Not resolved by this spike

Which option (A/B/C), where the toggle or indicator lives in the UI, how a status-less
node should render distinctly from "waiting"/"not yet run" (which already exist as
concepts elsewhere in the graph and shouldn't be visually confused with "no execution
data exists at all"), and whether the registry's last-write timestamp is reliable enough
across every deploy path to support option B's automatic comparison. This needs a product
decision (Principle #6 — this touches what the pipeline page's default view means) before
any UI code; the backend needs no changes under any of the three options.

## Resolution

**Option A** — an explicit toggle, but merged into the existing History button rather
than added as a separate pair of tabs: a "Structure" button forms one seamless control
with the History button (the one showing the execution count, e.g. "📖 4") — shared
border, no gap, an internal divider, reading as one control with two states rather than
two independent buttons. The History button keeps its exact pre-existing job (opens the
execution picker, lists every run) and additionally serves as the visual "active"
indicator for run mode. Defaults to "run" always; never silently changes what an existing
view means (ruling out option B's automatic-swap risk).

**What answered the two open questions from this spike, once actually built:**

- **Status-less node rendering was not a gap after all.** `DAGGraphFlow` already had a
  complete, unused-elsewhere-for-this-purpose `isBlueprint` prop (dashed border, reduced
  opacity, "Not yet executed" label, hidden legend) — built for a different existing case
  ("no executions for the selected date, but the DAG has nodes"). "Current structure"
  mode's `tasks.length === 0` naturally routes into the same existing render branch; no
  new node-rendering code was needed.
- **A real gap surfaced that this spike didn't anticipate:** `/pipeline-status` falls back
  to a **same-day scan** when `pipeline_execution` is omitted — not empty. Forcing only
  `pipeline_execution` to null (as originally planned) would have let an earlier run's
  *real* task statuses leak onto the newly-deployed structure, a confusing hybrid rather
  than a clean skeleton. Fixed by forcing `tasks = []` client-side whenever the toggle is
  on 'current', regardless of what the status endpoint returns — 'current' means
  structure only, never execution data, by construction, not by hoping the two endpoints'
  fallbacks happen to agree.
- The toggle's own state (`dagViewSource` in `useAppStore`) resets to `'run'` when
  switching pipelines (centralized in the `setSelectedPipeline`/`setSelectedExecution`
  store actions themselves, not scattered across every click handler that might call
  them — a `navigateToExecution` path from notifications/AllRuns bypasses the sidebar
  entirely, which an earlier per-click-handler version of this reset missed) and when
  picking a specific execution from the History picker.
- **UI iteration:** the first design used two separate pill buttons ("Latest run" /
  "Current structure") next to the DAG/Gantt/Calendar tabs. Feedback: a standalone
  "Latest run" label is misleading once you can pick any past execution from the History
  dropdown, not just the latest — and a separate toggle duplicated the already-existing
  History button's job of indicating "which run am I looking at." Merged into the single
  control described above instead.
- Clicking a blueprint node was found, in review, to still fall through to a fake
  `{status:'waiting', pipeline_name:'', execution_name:''}` task object and open the
  detail modal with misleading data — fixed by skipping the modal (and switching the
  node's cursor to `default`) whenever `isBlueprint` is true.
- The two independent implementations that had accreted around "build DAG nodes for
  visualization" (`generate_dag_json` and `_build_pipeline_metadata`'s `dag_metadata`)
  were unified into one shared function while fixing the task-type badge gap — see
  ADR #119.
