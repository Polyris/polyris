Status: implemented in 0.100.0 — see `CHANGELOG.md` and ADR-123 §5. Kept here as the design rationale.

# Task detail UI — upstream, output, and manual-intervention rendering

You have two live issues in the Task Detail modal's Input / Output tab:

1. A `_manually_resolved` marker written by mark-success/skip/fail is visually
   indistinguishable from an organic run — downstream `xcom.get(dep)["rows"]`
   crashes because the marker is not real data, and neither the upstream card
   on the consumer nor the Output card on the resolved task itself flags the
   intervention.
2. Card weight is inconsistent: organic success gets a clean left-stripe;
   failed / skipped / no-output / truncated / S3-ref get full-bleed coloured
   banners. Fan-ins with mixed statuses read as noise.

This spike surveys prior art in six orchestrators and proposes a single card
system that fixes both.

## 1. Prior art

| Tool | Upstream deps | Output display | Manual-intervention indicator | Status colour grammar |
|---|---|---|---|---|
| **Airflow 3** | No dedicated upstream panel in the Task Instance modal — you click upstream cells in the Grid view; Details tab shows metadata only, XComs live on their own tab (key/value/task/dag columns, copy button per row). | XComs tab: table of XCom rows returned or pushed; click a row to expand the value; large payloads truncate with a "view full" link. | State is set to `success`/`failed`, and the action lands in the Audit Log (Browse → Audit Logs) with the operator name and the note field the operator fills in before confirming. No badge on the task cell itself — greenness looks organic. | Success green, running lime, failed red, upstream_failed orange, skipped pink, retry gold — one hue per state, applied as a solid cell fill in Grid view and as a coloured chip in the modal header. |
| **Prefect 3** | Task Run detail links to parent Flow Run and to input parameters; upstream task outputs are not surfaced as a first-class panel — you navigate. | Results section shows the return value (or `PersistedResult` pointer) with a JSON viewer; large results collapse to a summary line with byte size. | Manual state overrides use dedicated state names — `Cancelled`, `Paused`, `Suspended`, plus custom `Completed` sub-names like `Cached` / `RolledBack`. The state chip carries the exact name, so "manual" is a state, not a decoration. | 19 states across 8 colour families (green completed, red failed, orange crashed, blue running, purple pending/paused, yellow scheduled, dark-grey cancelled). All chip-shaped, same weight — no full-bleed banners. |
| **Dagster** | Op detail shows an "Inputs" panel that lists upstream asset keys with a status pill and a link to the last materialization; asset lineage graph carries the same pills. | Outputs panel per op step lists each output name with type, metadata rows, and a "value preview" (only when the op explicitly logs it). Assets show a Metadata table (numeric values, markdown, plots). | `report_runless_asset_event` and the Report Materialization dialog write a materialization event with `MANUAL` in the source badge (grey pill on the timeline point). The timeline dot uses the same green as an organic materialization; the badge is the differentiator. | Green materialized, red failed, grey skipped/observed, blue in-progress. All as small pills on a timeline; the run-timeline never uses a full card as a status container. |
| **Temporal** | Workflow detail's Compact view groups related events (`ActivityTaskScheduled` + `Started` + `Completed`) under one expandable node; inputs and outputs live inside the node body as side-by-side JSON blocks. | Same node body: `Result` section renders the JSON payload, with a size counter and a download link when large. | Reset creates a new Workflow Execution and links back to the reset point in the original — the new execution's badge reads `Reset from event N`. Signals appear as their own event group with a purple icon. Manual completions never masquerade as organic ones. | Green completion, red failure, dashed red retrying, dashed purple pending, solid purple signal/update, grey terminated. Colour lives on the dot / connector, not the card fill. |
| **AWS Step Functions** | Step details pane has Input / Output / Details / Definition / Retry / Events tabs; input rendered as pretty JSON, an "Advanced view" toggle shows the InputPath → Parameters → ResultSelector → OutputPath transform. | Output tab: pretty JSON, error icon on tab header when the state failed. | No mark-success. Recovery is Redrive (rerun from the failed step) or Start New Execution — both create a new execution rather than mutating the current one. Manual mutation is not part of the model. | Graph legend: green succeeded, red failed, blue running, grey not started, orange caught error; table view timeline column uses the same hues as segments. Cards themselves are neutral; colour is on the state node and the tab-header error icon. |
| **GitHub Actions** | Job graph shows upstream jobs as clickable nodes; a failed upstream shows as a red node with a broken-line connector to the skipped downstream. | Step log is a collapsible section per step; failed steps auto-expand, with the failing line highlighted. No structured "output" — everything is stdout. | "Re-run failed jobs" is the only intervention; there is no mark-success. Re-runs create a new attempt with an attempt-N badge on the run header. | Green check, red X, grey dash (skipped), yellow dot (running). Icons carry the state; row background stays neutral. |
| **polyris today** | Upstream cards under a "Upstream (N)" collapsible; success = green left stripe on `<details>`; skipped / failed / aborted = full-bleed red banner; no-output = full-bleed yellow; S3-ref = full-bleed muted. | Single OutputCard: success = green stripe + click-to-expand; truncated / empty / aws-metadata = full-bleed warn or muted. | `_manually_resolved: true` marker written to DDB output; UI shows it as raw JSON in the same green success card. No badge, no operator name, no reason surfaced. | Left-stripe green for success; full-bleed red/yellow/muted for everything else. Two visual weights competing on one screen. |

## 2. Recurring patterns

**A. Colour lives on a stripe, dot, or chip — never on the card fill.** Airflow's
Grid view is the one holdout that fills whole cells, but its detail modal
switches to chip-on-header. Dagster, Temporal, Step Functions, GitHub, and
Prefect all keep the card body neutral and put the status in a small, fixed-size
signal so a mixed-status list stays scannable. polyris's full-bleed banners are
an outlier.

**B. Manual interventions carry a dedicated state or badge — never a silent
green.** Prefect promotes manual paths to first-class state names (`Cached`,
`RolledBack`, `Cancelled`). Dagster's Report Materialization dialog stamps a
`MANUAL` pill on the timeline point. Temporal's Reset creates a new execution
with a `Reset from event N` breadcrumb. Airflow is the closest to polyris's
current problem — the cell just turns green — but it records the operator and
note in the Audit Log so intent is at least recoverable. Nowhere else does a
manual completion render identically to an organic one with no signal.

**C. Operator intent goes in a note field, and the note is surfaced next to
the state.** Airflow's Note is the model here; Prefect exposes its `resume`
input schema; Temporal shows the reset reason string. polyris already stores
`_reason` on the marker — nothing renders it.

**D. Large / offloaded payloads are always signposted, but as metadata, not as
a banner.** Every tool shows a byte size plus a link/CTA (Temporal: download;
Dagster: metadata pointer; Airflow: view-full; Step Functions: none, hard 256 KB
cap). polyris's yellow full-bleed for truncated / S3-ref treats a payload
pointer as an error condition — it's a state, not an error.

## 3. Recommendation

### Unified card

One card shape for upstream deps and output, in three visual weights driven by
severity, not by whether the payload was inline vs offloaded. Every card is:

  - a bordered rectangle with a **4 px left stripe** for status colour
  - a header row: `[status icon] [name] [primary badge] [secondary badge?] [copy] [chevron]`
  - a body that expands on click to the JSON block (via existing
    `CollapsibleJsonBlock`), plus an optional inline detail line for
    non-obvious states (truncation, S3 ref, manual reason).

Full-bleed backgrounds are removed entirely — they're the current source of
noise. The stripe carries the colour; the badge carries the label; the icon
carries the shape. `td-banner--warn` / `--error` / `--muted` become the
stripe + badge combo, not a fill.

**State × colour × icon × primary badge × secondary badge:**

| Card state | Stripe | Icon | Primary badge | Secondary badge | Expandable |
|---|---|---|---|---|---|
| Organic success | green | CheckCircle2 | `success` | — | yes (JSON) |
| Organic skipped | pink | SkipForward | `skipped` | — | yes (JSON, if any) |
| Organic failed / aborted / upstream_failed | red | XCircle | `failed` (or exact status) | — | yes (JSON + error) |
| **Manual mark-success** | green | CheckCircle2 | `success` | `manual` (blue) | yes (reason + expand for raw marker) |
| **Manual skip** | pink | SkipForward | `skipped` | `manual` (blue) | yes (reason + raw marker) |
| **Manual fail** | red | XCircle | `failed` | `manual` (blue) | yes (reason + raw marker) |
| No output recorded | grey | Database | `empty` | — | no |
| Truncated (inline cap) | amber | AlertTriangle | `truncated` | shows `> N KB` | no (metadata only) |
| S3-ref (claim check) | grey | Database | `s3` | shows path | no (metadata only) |
| Malformed | amber | AlertTriangle | `malformed` | — | yes (raw JSON) |

The stripe uses the same three tokens the CSS already exports (`--success`,
`--warning`, `--error`) plus one new `--info` for the manual badge. No new
palette required.

### Rendering the `_manually_resolved` marker

Detect the marker before rendering any card:

```
looksManuallyResolved(output) === true iff
  output is object AND output._manually_resolved === true
```

When true:

  - stripe + icon + primary badge follow the **resolution** (`success`,
    `skipped`, `failed`), not the marker's shape
  - append a `manual` secondary badge (blue chip, small)
  - card body replaces the raw marker JSON with a single line: **"Marked
    <resolution> by operator — <reason or 'no reason given'>"**
  - a `<details><summary>Marker</summary>…</details>` block gives access to
    the raw `_manually_resolved` / `_resolution` / `_reason` payload for
    debugging; not shown by default
  - if the backend records the actor (e.g. Cognito sub, PAT owner), include
    the display name in the body line — otherwise omit gracefully.

### Consistency across both surfaces

The same detection and rendering apply to:

  - **Upstream card** on any consumer task (this is the source of the
    downstream crash today — the badge tells the reader "don't
    `xcom.get(dep)["rows"]` — it's a marker")
  - **Output card** on the resolved task's own Input / Output tab (same badge,
    same body line — the operator's own view is honest about what they did)

Both use `OutputCard` and `UpstreamDep` — one shared helper
`renderManualResolution(marker)` returns the body line + collapsible raw block,
called from both.

### ASCII mockups (one line each)

```
[|] CheckCircle2  extract_users            success                     [copy] [>]
[|] SkipForward   backfill_2026            skipped                     [copy] [>]
[|] XCircle       compute_metrics          failed                      [copy] [>]
[|] CheckCircle2  extract_users            success  manual             [copy] [>]
[|] SkipForward   backfill_2026            skipped  manual             [copy] [>]
[|] Database      write_partition          empty                              (no toggle)
[|] AlertTriangle heavy_output             truncated  > 42 KB                 (no toggle)
[|] Database      offloaded_dataset        s3  s3://bucket/key                (no toggle)
```

Where `[|]` is the 4 px status stripe. The visual line stays flat across every
state — a fan-in of 8 mixed cards reads as one column, not a colour siren.

## 4. Trade-offs considered and rejected

**Keep full-bleed banners for failure, add subtle blue tint for manual —
rejected.** This preserves the current visual disparity that motivates the
spike, and layering a manual tint over a red fill turns cards muddy. The whole
point is one weight, not two.

**Add a fourth tab "Manual actions" listing every intervention on this task —
rejected for now.** Useful for audit but doesn't solve the in-context problem
(the consumer reading its upstream card still sees "success" with no signal).
An audit tab is a good follow-up once the badge exists; it isn't a
substitute.

**Rename the resolution states (`manual_success`, `manual_skip`) so status
alone tells the story — rejected.** Would require SDK, generators, and DDB
migrations, plus new trigger-rule semantics (does `all_success` accept
`manual_success`? probably yes — but every consumer decides). The badge lives
in the UI and touches nothing else. If a future ADR wants first-class states
(Prefect's approach), the badge is a stepping stone, not a blocker.

## 5. Implementation scope

Rough delta (UI only — the marker already exists in DDB):

  - `OutputCard.tsx` — add manual detection + badge + body line; drop
    `awsMetadata` full-bleed in favour of stripe + badge. ~40 LOC.
  - `TaskDetailModal.tsx::UpstreamDep` — same detection; replace the six
    banner variants with the unified card shape. ~80 LOC delta (mostly
    deletion).
  - New `manualResolution.ts` helper (detection + body-line builder + raw
    marker toggle). ~30 LOC, plus 40 LOC of tests.
  - `_modals.css` — retire `.td-banner--warn/--error/--muted` fills, keep
    the stripe rules, add a `.td-badge--manual` (blue chip). Net negative
    CSS (~30 lines removed, ~10 added).
  - `TaskDetailModal.test.tsx` — six new render cases (organic × 3, manual
    × 3) plus the truncated / S3-ref / empty regressions. ~120 LOC.

**Bundle vs split:** the two changes decouple cleanly.

  - PR 1: unified card shape (fixes issue #2, no marker logic). Safe,
    visual-only, all existing tests pass with class-name updates.
  - PR 2: manual-resolution badge + body line (fixes issue #1). Depends on
    PR 1's card shape but is otherwise self-contained.

Shipping PR 1 first is the right order — the badge lands on a card system
that already has a place to put a secondary chip, rather than fighting the
current banner geometry.

## Resolved decisions (historical)

- **Operator identity on the marker?** Decided **yes, ship with the UI change**
  in the same PR. `console_api/routes/tasks.py::_write_synthetic_output_marker`
  now writes `_operator` alongside `_manually_resolved` / `_resolution` /
  `_reason`. `Principal` gained an `email` slot; `auth.operator_display(event)`
  resolves it to email → sub → `pat:<name>` → `"unknown"`. UI reads the field
  through `manualResolution.ts::detectManualResolution`, falls back to the
  generic label `"operator"` for pre-0.100.0 records. See ADR-123 §5 and
  CHANGELOG 0.100.0.
