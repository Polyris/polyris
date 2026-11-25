# ADR #55 — Scheduled Pipeline Runs — direct EventBridge → SFN, not routed through bulk-backfill (v0.78.0)

#### Context

EventBridge cron rules currently invoke pipeline SFNs directly. The
v0.78 redesign asks: should scheduled (cron-triggered) executions also
create Backfill records, routing through `polyris-bulk-backfill`?

Question #6 was discussed in detail. Conclusion (from user feedback):
preserve existing `/runs` UI which lists all executions including cron;
add `/backfills/{id}` as a new drill-in for bulk operations only.
Scheduled runs are **not** Backfills.

#### Decision

EventBridge → SFN direct path remains unchanged. Scheduled pipeline
executions create regular `pipeline-tokens` rows with `record_type` =
absent (or "execution" by convention; UI treats absence as execution).
They do **not** have `backfill_id`. They do **not** appear in
`/api/backfills` list. They appear in `/runs` page as before.

##### Rationale

1. **UI habit preservation.** Existing `/runs` page is the canonical
   "what ran today" view. Users associate the URL with this data. Moving
   cron-triggered executions out of `/runs` (e.g., to `/executions`)
   would break habits.
2. **Cron + manual + asset-event flows are conceptually "executions",
   not "backfills".** Each is a single materialization. Backfill is
   explicitly a bulk operation (1+ partitions intentionally batched).
   Conflating them removes the value of distinct concepts.
3. **No code path needs unification.** Cron triggering is one
   EventBridge → SFN call. Bulk-backfill is a Map of N such calls.
   Wrapping a single call in a bulk-backfill SFN would add ~$0.01 per
   scheduled run × 600 scheduled/month × 20 pipelines = ~$120/month
   overhead for no functional gain.
4. **Lineage from Backfill cascade is preserved differently.** When a
   Backfill triggers downstream consumers via cascade=auto/all, those
   consumer executions get `cascade_source_backfill_id` (ADR #51,
   Question #7/A). This connects scheduled-like executions (event-triggered)
   back to their source bulk-backfill without making them Backfills
   themselves.

##### What's deferred (not done in v0.78)

If at some point we want full symmetry — every execution traces back to
a Backfill record — that's a future ADR. Not now. Concrete reasons:

- /runs page would explode in volume (one Backfill row per cron run);
  filtering and pagination behavior would need redesign
- Cancel/retry-failed UI on scheduled-source backfills is mostly
  meaningless (cron will retrigger tomorrow anyway)
- Manual one-day runs (today's `useRunPipelineMutation` → maps to
  Backfill `partitions=[today]`) are the explicit case where a user
  initiated a single execution — they get a Backfill record. Scheduled
  is by definition not user-initiated.

##### What `/runs` shows

| Source | record_type | backfill_id | Shown in /runs |
|---|---|---|---|
| Cron trigger | absent | absent | Yes (as today) |
| Manual one-day run | absent | bf-X (its own Backfill) | Yes (with link to /backfills/bf-X) |
| Manual backfill (date range) | absent | bf-X | Yes (each partition row with same bf-X) |
| Asset event trigger (org→consumer) | absent | absent | Yes |
| Cascade-triggered (from Backfill) | absent | absent + cascade_source_backfill_id=bf-X | Yes (with link to /backfills/bf-X for cascade source) |
| Force-trigger | absent | bf-X (single-day Backfill) | Yes |
| Backfill record (sentinel) | "backfill" | bf-X (==execution_name) | **Excluded** from /runs (filtered out by record_type) |

UI filter on /runs: `WHERE record_type != "backfill"` or absent. This
excludes only the Backfill summary rows; all execution rows visible.

UI filter chip "Backfill" allows quick narrowing: shows only executions
with non-null `backfill_id`.

##### What `/backfills/{id}` Detail shows

Backfill drill-in shows only that Backfill's execution rows (filtered by
`backfill_id == id`), plus cascade-triggered executions (filtered by
`cascade_source_backfill_id == id`). Cron-triggered or other unrelated
executions are not shown.

#### Migration

No migration. Existing EventBridge configuration unchanged. Existing
pipeline SFN definitions unchanged. Only new is that some executions
have `backfill_id` attribute set (Backfill-initiated) and some have
`cascade_source_backfill_id` (cascade-triggered). Both are sparse new
attributes; legacy executions without them work normally.

`is_internal_record()` in `utils.py` is updated to recognize Backfill
records (sentinel `pipeline_name=_polyris_bulk_backfill` AND
`record_type=backfill`) and exclude them from execution-list iterations.
Without this update, Backfill records would leak into UI counts. (See
Discovery Phase B, file `utils.py` entry.)

#### Out of scope

- Future symmetry where all executions get a Backfill ancestor — not
  rejected, just not in v0.78. ADR amendment if/when needed.
- Cron rule management UI (pause cron, edit schedule) — separate feature,
  unrelated.

#### Cost

Zero. No new infrastructure. Logic-only changes in `utils.py` and UI
filter; no AWS dimension affected.
