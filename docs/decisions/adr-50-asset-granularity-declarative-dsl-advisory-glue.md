# ADR #50 — Asset Granularity — declarative DSL + advisory Glue auto-detect + drift detection (v0.77.0)

**Status:** Accepted.
**Date:** May 2026.

#### Context

ADR #49 shipped the Asset Matrix as a daily-only operational view —
columns are calendar dates, cells are task status on that date. After
deployment we received feedback from tech lead Myroslav: granularity
is hardcoded as day, but reality is more complex — some pipelines run
weekly, twice daily, hourly. How should the matrix show multiple
partition values per day? How to aggregate? How does the filter let
operators choose which to display?

This was a fair critique — v0.76 made an implicit assumption (every
asset is daily) that wouldn't survive contact with non-daily pipelines.
We needed a path forward without descending into Dagster-level
complexity (Multi-dimensional Partitions, Dynamic Partitions, etc.).

#### Decision

Add a **declarative `granularity` field** to the `Asset` DSL, defaulting
to `"daily"` so existing code keeps working with zero boilerplate. The
allowed set is closed: `hourly | daily | weekly | monthly`. Anything
more exotic (quarterly, region-partitioned, dynamic) stays out of scope
until concrete user demand appears.

```python
Asset("acme/orders")                                  # default daily
Asset("acme/weekly_summary", granularity="weekly")
Asset("acme/monthly_report", granularity="monthly",
      partition_start="2024-01")
Asset("acme/hourly_events", granularity="hourly")
```

The matrix endpoint accepts a `granularity` query param and renders
columns in the corresponding format (`YYYY-MM-DD` / `YYYY-Www` /
`YYYY-MM` / `YYYY-MM-DDTHH`). The frontend gets a granularity dropdown.
Cells carry per-status counts (`partition_count`, `success_count`,
`failed_count`, `running_count`) so tooltips surface hidden re-runs.

Three companion features round out the change:

1. **Filter mode for cross-granularity views.** The matrix shows only
   assets matching the requested granularity. Other assets are hidden
   — UI dropdown switches. Keeps the column axis consistent without
   "spanning cells" complexity (one row's cell covering 7 columns to
   represent a week). Unified multi-granularity view feasible but
   deferred to v0.78 if real users ask.

2. **Glue auto-detect at deploy time.** For Glue-backed assets,
   `polyris-deploy` reads `PartitionKeys` and infers granularity from
   naming: `year/month/day` → daily, `year/month/day/hour` → hourly,
   `year/week` → weekly, `year/month` → monthly. Inference is
   **advisory** — declared values win, but mismatches print warnings.

3. **Drift detection** via new endpoint `GET /api/assets/drift`.
   For each asset with declared granularity, count successful
   materializations over last 30 days and compare to expectations
   (`hourly:720, daily:30, weekly:4, monthly:1`). Severity:
   `≥0.5` → healthy, `0.25-0.5` → warning, `<0.25` → critical.
   UI binds the result to a per-row badge.

#### Why declarative (not auto-detect)

Both Dagster (`partitions_def`) and other orchestrators chose declarative despite their larger engineering teams. Their
reasoning matches ours: partition cadence is *intentional* (the user's
design choice), not observable. A pipeline that hasn't run for a week
looks "monthly" to an inference engine but might be daily-and-broken.

We chose declarative as source of truth, hybrid for DX:

  - **Default = `"daily"`** — zero-boilerplate simple cases. The vast
    majority of batch ETL is daily, and `Asset("name")` keeps working.
  - **Glue auto-detect is advisory** — confirms or contradicts
    declaration at deploy time; never overrides. Unconventional naming
    gets a warning users can ignore.
  - **Drift detection** — runtime "did the user lie?" check that
    complements deploy-time Glue check. Catches *behavioral* drift
    (declared weekly, no materializations for two months) that Glue
    metadata can't see.

#### Why filter mode (not unified)

Three options considered for cross-granularity rendering:

  - **A — Filter mode (chosen).** One granularity at a time; others
    hidden. UI dropdown switches.
  - **B — Unified view.** All granularities in one matrix; weekly cells
    span 7 daily columns. Visually richer but introduces "cells of
    different sizes" complicating rendering and click handlers.
  - **C — Per-asset granularity axis.** Each row has its own axis.
    Hardest to read, defeats the purpose of a 2D grid.

Filter mode is simplest correct answer for v0.77. If users ask for B,
it's an additive change — endpoint already returns granularity per row.

#### Why drift detection (not "trust the user")

Without drift detection, silent daily default is dangerous: user who
forgot `granularity="weekly"` sees weekly asset as 6 missing + 1
materialized every week. Technically correct but misleading — they'll
think their pipeline is broken.

Drift detection catches this: 4 weekly materializations vs 30 daily
expectation → ratio 0.13 → critical drift badge. Tooltip suggests
"update the asset's granularity declaration" — exactly the right fix.

Cost: one Scan + 30 parallel Queries per drift refresh (5-min polling).
Small project: negligible (~$0.13/month per 100 customers).

#### Backward compatibility

  - **Asset DSL:** `granularity`/`partition_start` are kwargs with
    defaults. Existing code unchanged.
  - **pipeline_registry:** v0.77+ records include `granularity`;
    pre-v0.77 records default to "daily".
  - **Matrix endpoint:** `granularity` query param defaults to daily.
  - **Cells:** new `partition_count`/`success_count`/`failed_count`/
    `running_count` fields — clients ignoring extras keep working.

No SFN template changes. No DDB schema changes. No new tables.

#### Out of scope for v0.77 (explicit non-goals)

  - Multi-dimensional partitions (region × date) — Dagster has, edge
    case for our target audience
  - Dynamic partitions (sensors creating runtime keys)
  - Static partitions (per-region/customer slices)
  - Unified multi-granularity view — deferred to v0.78 if requested
  - Data quality on cells — separate subsystem; integrate with Great
    Expectations or dbt tests if/when shipped
  - `partition_value` ≠ `execution_date` semantics — deferred to v0.79,
    requires SFN template change
  - Quarterly/yearly granularities — YAGNI

#### What the user sees

Hover the warning triangle:
> "Declared daily, but only 4 materializations in 30 days
> (expected 30). Check your pipeline or update the asset's
> granularity declaration."

Hover a cell with re-runs:
> "acme/orders @ 2026-05-12
>  Materialized at 20:00 UTC
>  Source: acme-daily.extract
>  Runs today: 3✓ (3 total)"
