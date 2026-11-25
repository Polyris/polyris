# ADR #47 — Asset Lineage `last_updated` Enrichment — Two Endpoints, Two Scopes

**Status:** Adopted in v0.75.5.

**Context:** The Console UI has two distinct needs related to asset
materialization timing, and a single endpoint would have to pick one:

  1. **Date picker scope** — "show me events that happened on May 7":
     drives the catalog "Recent Events" panel, the per-day execution
     filter, and date navigation. Needs a query indexed by date.
  2. **Catalog Status column** — "for this asset, when did it last
     materialize, ever": drives the Status column on the asset list
     and the freshness banner on the detail page. Needs the latest
     event per asset, independent of any date filter.

A single `/recent-events?date=...` endpoint conflated both. The
catalog Status column read from the same date-scoped result, so when
an operator opened the UI on a day with no executions yet (e.g. after
a fresh deploy + backfill, before the first scheduled run), every
asset rendered as if it had never run — even assets with thousands
of historical events visible on their detail pages. v0.75.3 made this
worse by substituting "Never" for the calculator's honest "No data"
label, an over-confident assertion that was demonstrably false.

**Decision:**

  - Keep `GET /api/assets/recent-events?date=YYYY-MM-DD` exactly as it
    was — date-scoped query against the asset-events GSI
    (`date-index`, key=`execution_date`). Powers anything that wants
    "what happened on day X".
  - Extend `GET /api/assets/lineage` to stamp each asset entry with
    `last_updated`: the ISO timestamp of the most recent event for
    that asset, looked up directly via the primary key
    (`asset_name` HASH + `event_time` RANGE, descending, LIMIT 1).
    Independent of any date scope. Powers the catalog Status column
    and the detail-page banner fallback.
  - The frontend staleness calculator gains a third resolution path
    that consumes this fallback: `recentEvents[name]` →
    `asset.last_updated` → unknown. Cell rendering reverts to the
    natural `staleness.label` (no `stalenessText` substitution).

**Why not "always return last_updated everywhere"?** The cost shape
matters. `last_updated` enrichment is N point-lookups (N = asset
count), parallelised, ~5ms each. For the 23-asset catalog Mike runs,
that's 50-100ms RTT-bounded — acceptable on a list-page load. Adding
the same enrichment to per-event responses (e.g. in the events list)
would multiply DDB reads by event count without UI benefit; the
events list already shows event_time per row.

**Why not pre-compute `last_updated` at write time and store it on
the pipeline_registry asset record?** Considered, rejected:

  - Write path would need to update two tables atomically, doubling
    write cost on every materialization
  - DynamoDB doesn't offer multi-table transactions cheaply
  - The read-side lookup is already cheap (~5ms PK query) so the
    write-time optimization saves negligible read time at the cost
    of write-time complexity and edge cases (transactional rollback,
    last-writer-wins races between concurrent task completions)

Read-time enrichment is the right tradeoff here — write path stays
simple, read cost is bounded.

**Why parallel point-lookups instead of a single GSI scan grouped by
asset?** The GSI we have (`date-index`) is keyed on `execution_date`,
not `asset_name`. Adding a second GSI keyed on asset would duplicate
storage and cost ~$0.25/GB/month for the asset-events table, which
grows unbounded. Per-asset PK lookup costs roughly the same per call
as the GSI alternative, parallelizes cleanly, and adds zero
infrastructure. Verified: 23 assets × 1 query @ ~5ms = ~5ms wall time
when fully parallelized, well within the Lambda's 30s budget.

**Resilience:** one failing lookup logs a warning and degrades to
`last_updated: ""` for that asset only. The endpoint never 500s
because of a single asset's transient DDB throttle. Tested in
`test_lineage_last_updated.py::test_one_failing_lookup_does_not_break_others`.

**Out of scope (deferred):**

  - **Caching the lookup map** — Lambda memoization across
    invocations would help but introduces stale-data risk (an asset
    materializes after a prior list-page load, the cached `last_updated`
    is now wrong). Skipped until measurable latency complaint exists;
    50-100ms is acceptable.
  - **Aggregated "last_updated" in pipeline_registry** — see above,
    rejected for write-cost / complexity reasons.
  - **Cross-region last_updated** — out of scope; Polyris is
    single-region per stack and `asset-events` table is per-stack.
