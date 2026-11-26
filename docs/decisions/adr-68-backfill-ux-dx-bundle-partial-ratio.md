---
area: ui
accepted: 2026-05-27
---
# ADR #68 — Backfill UX/DX bundle — partial ratio, retry chain, notification source

#### Context

User review of v0.78.10 surfaced three small but distinct gaps:

1. The `partial` status pill said only "partial" — no indication of how
   partial. A 4-of-5 success looked the same as 1-of-5. Users had to
   click into the detail page to see the ratio.

2. `retry_failed` stores a `parent_backfill_id` link on the new backfill,
   but the UI didn't surface it. Three rounds of retry produced four
   disconnected detail pages; the chain was visible only via grep on
   the DDB record.

3. Backfill terminal events emitted no notification. The `Notifications.tsx`
   bell + dropdown infrastructure already polled the
   `/api/notifications` endpoint every 30s for pipeline failures, but
   the endpoint didn't surface backfills at all. Users had to refresh
   the Backfills list manually to find out a backfill finished.

#### Decision

Bundle all three as one release (v0.78.11) because they share the
same domain (Backfill) and overlap in implementation cost:

**1. Partial ratio in pill** — purely cosmetic UI change:
`partial` → `partial (4/5)`. Done in two files (BackfillsListPage,
BackfillDetailPage). Other statuses unchanged.

**2. Retry chain on detail page** — two-direction linkage:
- *Upward*: `parent_backfill_id` already in the DDB record; just
  expose it in `_format_backfill_summary` and render as a clickable
  link.
- *Downward*: new repo method `list_retries_of(parent_backfill_id)`
  via DDB scan with filter (Backfills are infrequent — ~hundreds per
  pipeline per year — so scan is fine; if retry rate spikes, add a
  GSI). Exposed in detail endpoint as `retried_by[]`.

Rendered as a compact section between metadata and partition heatmap,
hidden entirely when neither parent nor children exist (avoids empty
section on first-time backfills).

**3. Backfill terminal events in notification feed** — extend
`get_notifications` to also fetch recent terminal backfills via
`backfills_repo.list_recent(100)`, filter to terminal statuses
finished within the query window, sort by `finished_at` desc, cap to
limit. New notification subtype `type='backfill'` with status-specific
icon coloring (green/red/amber/grey). Click navigates to detail page
via new `onNavigateBackfill` prop.

#### Naming: "Partitions" → "Backfilling dates"

Renamed the partition heatmap heading too — user feedback that
"Partitions" was technical jargon not aligned with how users actually
think about the operation. Internal data model still uses
`partition_keys` / `partition_*` field names; only the UI heading
changed. This is a small surface-only rename with no schema impact.

#### Trade-offs considered

- **Scan vs GSI for `list_retries_of`**: scan is O(N) on the
  backfills table; chosen because retry chains are exceptional events,
  not hot paths. If we add bulk-retry automation later (e.g.
  scheduled re-runs), revisit and add a `parent-backfill-id-index`
  GSI.

- **Notification source: poll vs push**: poll path is consistent with
  how pipeline failure notifications already work. A push model (e.g.
  EventBridge → notification table) would scale better and update
  faster, but adds infrastructure for a low-volume event class.
  Defer until backfill volume justifies it.

- **Bundling vs three releases**: three separate releases would have
  better changelog granularity but triple deploy cost. Bundling is
  safer when each piece is small and they share a code surface
  (BackfillDetailPage gets edits for all three).

#### What this does NOT do

- Per-partition retry/cancel/skip — that's still in BACKLOG for the
  v0.79.x extension of existing endpoints. Out of scope here.
- Cost-preview before launch — separate concern, separate release.
- Chat notification integration: the UI alone was asked for
  (bell + dropdown), no Slack.

#### Lesson

When a UX issue can be solved with backend extension + UI rendering,
prefer extending existing endpoints over creating new ones. Both
`_format_backfill_summary` and `get_notifications` got one new field
each + light render code — no new routes, no new tables, no new GSIs.
This stays consistent with the "extend, don't duplicate" principle
that the per-partition-cancel discussion landed on (CLAUDE.md hard
rule #12 — Maximize reuse).
