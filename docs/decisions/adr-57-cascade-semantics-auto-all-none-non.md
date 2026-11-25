---
area: engine
accepted: 2026-05-20
---
# ADR #57 — Cascade Semantics — auto / all / none with non-transitive propagation

#### Context

ADR #51 introduces `cascade` option for asset-target backfills. This ADR
specifies exactly what happens to downstream consumers under each
cascade mode, edge case handling, and the implementation pattern.

#### Decision

Three cascade modes, applicable only when `target.type == "asset"`:

```python
class CascadeMode(str, Enum):
    AUTO = "auto"   # Default. Emit normal asset events; consumers decide.
    ALL  = "all"    # Force-trigger direct consumers regardless of rules.
    NONE = "none"   # Suppress asset event emission entirely.
```

For pipeline-target backfills, `cascade` is invalid (400 error).
Cascade applies only when materializing an asset, which by definition
emits a downstream-visible event.

##### Mode semantics in detail

###### `cascade=auto` (default)

For each partition's producer execution that succeeds, the normal asset
event is emitted. Downstream consumers receive the event via the existing
`notify_asset_consumers` SFN + `notify_asset_subscribers` Lambda
infrastructure. The consumer pipeline triggers (or doesn't) based on its
declared rules:

- `AssetRef(asset, freshness_hours=N)` — consumer triggers only if event
  age ≤ N hours. For backfill events with `event_time` in the past
  (e.g., backfilling Jan 2024 events in May 2026), age >> N → consumer
  skipped silently.
- `AssetAll([A, B])` / `AssetAny([A, B])` — consumer triggers when AND/OR
  condition over assets is met. Backfill partial coverage may not satisfy
  AND.
- `asset.consecutive(days=7)` — consumer requires 7 consecutive days
  materialized. Backfill of 3 of 7 days won't trigger consumer.

This is **respect the contract the user declared**. If user wrote
`within(hours=6)`, they meant it. Backfill should not bypass it silently.

Lineage: cascade-triggered consumer executions get
`cascade_source_backfill_id` set in their input → DDB. UI shows the link.

###### `cascade=all`

Producer runs; on success, asset event is emitted with an extra flag
`_cascade_all=true`. The receiving `notify_asset_consumers` SFN
recognizes this flag and **force-triggers** direct consumers, bypassing:

- Freshness checks (`within()`)
- AND/OR conditions (`AssetAll`, `AssetAny`)
- Consecutive day requirements (`consecutive()`)

Consumers receive `is_cascade_forced=true` in their input → tasks can
detect cascade-forced runs and adjust behavior (skip notifications,
write to alternate prefix, etc.). New input field.

Force-triggered consumers carry `cascade_source_backfill_id` same as
auto mode.

##### `cascade=none`

Producer runs; on success, asset event emission is **suppressed**. No
notification to consumers, no event row written to AssetEvents table.

Implementation: producer execution input carries `_suppress_asset_event=true`
flag. The `notify_dependents` step in the pipeline SFN template checks
this flag and skips its emission state.

Use case: silently re-materialize historical data without disturbing
downstream pipelines. Useful for data corrections that user wants
isolated.

##### Non-transitive cascade

Cascade applies only to **direct consumers** of the backfilled asset.
Transitive propagation through asset chains is not done by cascade —
each consumer's own asset emissions follow normal rules.

Example: `asset_A → consumer_B (produces asset_B) → consumer_C`.

Backfill on asset_A with cascade=all:
1. asset_A's producer runs for each partition
2. consumer_B is force-triggered (per cascade=all)
3. consumer_B succeeds → emits asset_B event normally (no cascade flag)
4. consumer_C reacts based on its own rules to asset_B event (no force)

If user wants asset_C also force-rebuilt, they backfill asset_C
explicitly. This is intentional bounded scope — prevents "cascade=all on
upstream of long chain accidentally triggers 50 downstream pipelines."

##### Multi-producer asset

If an asset has more than one producer pipeline (e.g., `acme/orders`
produced by both `daily_etl` and `manual_correction`), backfill on that
asset returns 400 `multi_producer_asset` with the producer list:

```json
{
  "error": "multi_producer_asset",
  "message": "Asset 'acme/orders' has multiple producers",
  "producers": [
    {"pipeline": "acme/daily_etl", "outlets": [...]},
    {"pipeline": "acme/manual_correction", "outlets": [...]}
  ],
  "suggestion": "Use target.type=pipeline with a specific producer"
}
```

User must pick. No multi-producer auto-pick or sequential run. Future
ADR can add UI helper to pick from the list and re-submit.

##### Edge cases matrix

| Scenario | auto | all | none |
|---|---|---|---|
| Consumer freshness gate fails (old event_time) | skip silently | force-trigger | N/A (no event) |
| Consumer AND condition partial (1/2 deps materialized) | skip (await full) | force-trigger | N/A |
| Consumer consecutive(days=N) partial | skip (await full) | force-trigger | N/A |
| Consumer is paused | event queues; resumes when unpaused | force-trigger ignored (paused respects) | N/A |
| Consumer has no rules (always triggers on event) | trigger | trigger (same) | N/A |
| Consumer triggers other downstream | normal asset-event chain | normal asset-event chain | N/A |
| Backfill failure on producer partition | no event emitted for that partition | no event for that partition | no event |
| Asset has no consumers | no-op cascade | no-op cascade | no-op cascade |
| Consumer is the same pipeline as the asset's producer | logical loop; reject at validation | logical loop; reject | N/A (no event) |

##### Preview warnings

The `/api/backfill` response includes `warnings[]` for cascade-related
issues detected at pre-flight:

```json
"warnings": [
  {
    "code": "freshness_gate_will_fail",
    "consumer": "weekly_summary",
    "message": "weekly_summary uses .within(hours=6); events for partitions older than 6h won't trigger it."
  },
  {
    "code": "consecutive_partial_coverage",
    "consumer": "monthly_report",
    "message": "monthly_report uses .consecutive(days=7); selected range covers only 3 of 7 days for week 2024-W03."
  },
  {
    "code": "consumer_paused",
    "consumer": "alerts_pipeline",
    "message": "alerts_pipeline is currently paused; cascade events will queue until resumed."
  }
]
```

Warnings are informational; do not block submission. User decides
whether to proceed.

##### Asset event payload changes

Existing event payload (from notify_dependents SFN):

```json
{
  "asset_name": "acme/orders",
  "event_time": "2024-01-15T12:00:00Z",
  "source_execution": "acme-daily-2024-01-15-...",
  "source_task": "publish"
}
```

New fields added in v0.78:

```json
{
  ...existing,
  "partition_key": "2024-01-15",
  "_cascade_all": true,                       // present if cascade=all
  "_suppress_emission": false,                // never serialized; flag at producer
  "cascade_source_backfill_id": "bf-a1b2c3d4" // set if cascade-originated
}
```

`partition_key` is added unconditionally (every emission includes it for
matrix/drift compatibility).

##### Implementation locations

1. **producer execution input** (built by bulk-backfill SFN Build_Child_Input
   state): if `cascade=all`, set `is_cascade_all=true` in input.
   If `cascade=none`, set `_suppress_asset_event=true`. Both fields are
   sparse (omitted in normal scheduled runs).

2. **notify_dependents SFN template** (per-task emission): reads
   `_suppress_asset_event` flag from execution input. If true, skip the
   emission state. Add one Choice state at the start.

3. **notify_asset_consumers SFN template**: reads `_cascade_all` from
   event payload. If true, skip the freshness/AND check in its Choice
   states, proceed directly to force-trigger.

4. **Consumer execution input** (set by notify_asset_consumers):
   propagates `cascade_source_backfill_id` from event. New attribute
   in input.

5. **dependency_wrapper template** (per-task wrap of consumer): reads
   `is_cascade_forced` and `cascade_source_backfill_id` from execution
   input, writes both to DDB on first task event for the execution.

##### CSS / UI surface

In `/runs` row for cascade-triggered execution:

```
weekly_summary  success  5m ago  🔗 cascade from bf-a1b2c3d4
```

The 🔗 icon + bf-id link clickable, navigates to /backfills/bf-a1b2c3d4.

In Backfill Detail page Cascade Summary panel:

```
Cascade summary:
  Mode: auto
  Asset events emitted: 8 of 8 partitions
  Consumer triggers (live):
    weekly_summary    2 triggered / 0 queued / 0 skipped (freshness)
    monthly_report    0 triggered / 0 queued / 1 skipped (consecutive partial)
    alerts_pipeline   0 triggered / 5 queued (paused)
```

##### Out of scope

- Per-consumer cascade override (e.g., "cascade=all to weekly_summary but
  cascade=none to alerts_pipeline") — not supported. Cascade applies
  uniformly to all direct consumers of the backfilled asset.
- Cascade-aware retry-failed — retry-failed inherits the original
  cascade mode. Out of scope to change cascade per retry.
- Cascade depth limit (max transitive levels) — non-transitive by
  design, so no limit needed.

#### Testing

Pinned by `tests/integration/test_cascade.py`.

#### Migration

No migration. Cascade is a new option on `/api/backfill`. Existing
notify_dependents and notify_asset_consumers SFN templates are extended
with new Choice states; existing consumers (executing without cascade
input fields) work unchanged.

#### Cost

Zero infrastructure. New input fields are sparse, no cost. Choice states
in SFN templates add ~2 transitions per emission/notify; ~$0.00005 per
backfill incremental.
