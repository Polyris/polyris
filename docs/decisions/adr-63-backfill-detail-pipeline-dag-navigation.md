---
area: ui
accepted: 2026-05-27
---
# ADR #63 — Backfill detail → pipeline DAG navigation

#### Context

`BackfillDetailPage` rendered a heatmap of partitions and a table of
child executions, but neither was clickable. Users wanting to drill from
"backfill bf-xxx failed on 2026-05-22" into "what did the DAG look like
for that partition" had no path forward — they'd close the detail page
and manually navigate to Pipelines, find the pipeline, set the date.
Multi-step. Easy to forget which date you came from.

External UX review (Mike, 2026-05-27): *"коли клацію то перекидає на dag
сьогоднішнього дня, а було б добре на можливість переключитись"*.

#### Decision

Two new optional callbacks on `BackfillDetailPage`:

```ts
onPartitionClick?: (pipelineName: string, partitionKey: string) => void
onChildClick?: (pipelineName: string, executionName: string, partitionKey: string | null) => void
```

App.tsx wires both to deep-links into the Pipelines view:

| Click target | Resulting URL |
|---|---|
| Partition heatmap cell `2026-05-22` | `/pipelines/?pipeline=<name>&date=2026-05-22` |
| Child execution row | `/pipelines/?pipeline=<name>&execution=<id>&date=<key>` |

When the callback prop is absent, the cell/row renders non-clickable
(static heatmap behavior preserved for any future use cases like a
read-only embed).

#### Why URL-based, not dropdown

Considered: dropdown picker on PipelinesPage to switch between
backfill→partition combinations. Rejected:

- Duplicates URL state. PipelinesPage already drives DAG view via
  `?date=` / `?execution=` query params. Adding a dropdown that *also*
  drives the view creates two sources of truth that must stay synced.
- Breaks shareable URLs. Send a colleague "/pipelines/?pipeline=daily&date=2026-05-22"
  and they see your view. Send "/pipelines/?pipeline=daily" + "click the
  third item in the dropdown" — not shareable.
- More state, more bugs. The chosen direct-link path is one URL hop,
  no extra UI surface, no extra state machine.

#### Tests

Pinned by `BackfillDetailPage.test.tsx`.

#### Backwards compatibility

Pure additive. Existing call sites without the callbacks see the same
static heatmap as before. App.tsx provides callbacks → users get
clickability. No type changes break consumers.
