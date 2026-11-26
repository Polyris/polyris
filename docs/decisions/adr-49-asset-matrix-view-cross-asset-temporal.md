---
area: ui
accepted: 2026-05-12
---
# ADR #49 — Asset Matrix View — Cross-asset temporal grid

**Context.** Recurring operator feedback (per tech-lead interviews from
Feb 2026) asks for a "global picture of assets across time" — answer to
the question *"what's broken, and when did it start?"* The existing
catalog view shows assets as a list with current freshness; the lineage
view shows graph relationships. Neither shows the temporal × asset
cross-section needed for outage forensics, partition-status review, or
backfill scoping. The Matrix view fills that gap.

#### Design history — honest record of the iteration

The implementation went through two passes before landing on the right
architecture. Recording both because future maintainers will think to
revisit the same alternatives and deserve to see why we picked what
we did.

**Pass 1 (initially shipped, then reverted in same release):
event-sourcing approach.** Every task with outlets emitted three asset
events to `asset_events`:

  - `event_type='started'` written before task execution
  - `event_type='materialized'` written on success (already existed pre-v0.76)
  - `event_type='failed'` written on failure path

The Matrix endpoint queried `asset_events.date-index` per date and
derived cell status from the latest event per (asset, date). Conceptually
elegant — a single source of truth, time-ordered, naturally handling
regenerate scenarios as "latest event wins."

**Why we rejected pass 1:**

  1. *Cost.* +4 SFN transitions and +1 DDB write per task with outlets,
     for every run including successes. At SaaS scale (100 customers ×
     500 tasks/day) this added ~$165/month — material against the
     project's "dramatically cheaper than managed orchestrators" positioning. The
     transitions weren't doing real work; they were duplicating state.
  2. *CLAUDE.md #12 violation.* Task status is already canonical in
     `pipeline-tokens`, updated by `Update_Status_Running` and
     `Save_Success`/`Save_Failed` on the SFN happy paths. The new
     event_type field stored the same information twice, with no new
     information added. The principle "before creating anything new,
     actively search for what already exists" was not honored.
  3. *Risk.* Pass 1 modified the core `RunTaskHelperSfn` definition that
     runs on every task in every pipeline. The reward (matrix view)
     didn't justify the blast radius (any bug in the new states could
     affect every pipeline).

**Pass 2 (current implementation): state-based derivation.** The Matrix
endpoint is a *projection* over existing canonical state. Nothing is
written to support it.

Sources:

  - `pipeline-tokens.date-pipeline-index` — projects `status`,
    `task_name`, `finished_at`, `running_at`, `error` per task per date
  - `pipeline_registry.tasks[].outlets` — which assets a task produces
    (already loaded by `_build_assets_from_pipelines`)
  - `pipeline_registry.asset_schedule` — which assets a DAG consumes
    (used for Queued cells; already loaded by other matrix logic)

The SFN template is **unchanged from v0.75.8**. The matrix is purely a
read-side concern.

#### Cell types

Five states. Four are derived from task records in `pipeline-tokens`;
one is derived from `asset_schedule` declarations:

| Cell | Trigger |
|------|---------|
| 🟢 Materialized | Latest producer-task record for (asset, date) has `status='success'` |
| 🔴 Failed | Latest producer-task record has `status='failed'` (or `aborted`/`stopped` — defensive: non-happy paths display as failed) |
| 🟡 Running | Any producer-task record has `status='running'` (running beats older finalized to give operator real-time visibility) |
| 🟠 Queued | No producer-task record exists for (asset, date), but at least one consumer DAG has this asset in its `asset_schedule` |
| ⚪ Missing | No producer record and no consumer waiting (returned as `null` to the UI) |

**Stale is excluded** — staleness is a "now" concept and would
misrepresent historical truth at each date. The Catalog tab continues
to show staleness as before.

#### Regenerate semantics

When backfill re-runs a date, `pipeline-tokens` ends up with multiple
records for the same (task_name, date) pair (each `start_execution`
creates a fresh `execution_name`). Resolution:

  1. If any matching record has `status='running'` → cell is 🟡 Running
     (operator sees what's happening now, not stale history).
  2. Otherwise pick the record with the latest `finished_at` and use
     its status. Example: orig run succeeded at 10:05, backfill failed
     at 11:05 → cell is 🔴 Failed with the backfill's error message,
     because that's the latest reality.

This naturally handles success→failed regenerations without timestamp
merging across tables (which the rejected pass-2 alternative would have
needed if we'd kept `asset_events` as the materialized source and only
moved running/failed to derivation).

#### Internal record filtering (CLAUDE.md mandate)

`pipeline-tokens` also holds non-task records (`_pause_*`,
`_notify_warn_*`) used for pause state and infra warnings. CLAUDE.md
explicitly mandates filtering these from every loop. `matrix.py` uses
`is_internal_record(execution_name)` on every record returned by
`query_by_date` — see `_fetch_date_safely`. Without this, a
`_notify_warn_` record with a real `task_name` field would be picked up
during cell derivation and produce false failed cells.

#### Multi-producer assets

The reverse index `asset_to_tasks` (built once per request from
`pipeline_registry`) maps each asset to a *list* of producer tasks.
Rare but real: partitioned writes where multiple tasks contribute to
the same asset name. `_derive_cell` accepts records from any task in
the list and picks the latest by the resolution rules above.

#### Cost — measured outcome

At SaaS scale of 100 customers × 500 tasks/day, this implementation
adds approximately:

  - $0 SFN transition cost (no template changes)
  - $0 DDB write cost (no new writes)
  - ~$3/month DDB read cost (matrix endpoint queries `pipeline-tokens`
    and `pipeline_registry` once per render; React Query polls every
    30 s)

Versus pass 1's $165/month — roughly 98% saving. This is the kind of
gap that matters for the project's competitive positioning.

#### Endpoint surface

```
GET /api/assets/matrix?from=YYYY-MM-DD&to=YYYY-MM-DD&group=&include_views=false

200 OK:
  {
    "dates": ["2026-04-15", "2026-04-16", ...],
    "rows": [{
      "asset_name": "acme/orders",
      "group": "acme",
      "type": "asset",
      "cells": {
        "2026-04-15": {
          "status": "materialized",
          "event_time": "2026-04-15T10:05:00Z",
          "source_task": "extract",
          "source_dag": "acme-daily",
          "error": ""
        },
        ...
      }
    }],
    "summary": {
      "total_assets": 42,
      "materialized_count": 580,
      "failed_count": 2,
      "running_count": 1,
      "queued_count": 3,
      "missing_count": 102
    },
    "range": {"from": "2026-04-15", "to": "2026-04-28"}
  }
```

Range bounds: default 14 days, hard maximum 60 days. Parallel queries
fan out one-per-date (capped at 14 workers). Partial DDB failures on a
date degrade that date to missing cells; the rest of the matrix still
renders. AccessDenied re-raises and surfaces an IAM-fix hint per ADR #38.

#### Frontend

`AssetMatrixView` is a third tab alongside Catalog and Lineage. The
cell payload shape is identical to the v0.76 pass-1 attempt, so no UI
changes were needed when we replaced the backend. One added field
(`error`) is consumed by the failed-cell tooltip to show the failure
reason without requiring a click through to task detail.

Mobile responsiveness follows ADR #40: cell sizes shrink at ≤1024px,
≤768px (default range cut to 7 days), ≤480px (default range 5 days).
Per CLAUDE.md #10, all CSS uses BEM-prefixed globals (`amv-*` in
`_assets.css` and `_mobile.css`); no `.module.css` files were created.

#### Out of scope for v1

These are listed explicitly so they don't re-appear as "did we forget X?"
questions during future work:

  - Per-tenant permissions (waiting for SaaS launch)
  - Pre-computed daily snapshot (premature; current cost is fine)
  - SSE / WebSocket real-time updates (30 s polling is the project standard)
  - Cron-aware "scheduled but missed" distinction (today's Queued cell
    treats every date in range as a potential consumer-waiting date;
    refining this needs cron introspection)
  - Bulk cell select for batch backfill
  - Column-header click filters
  - CSV / image export
