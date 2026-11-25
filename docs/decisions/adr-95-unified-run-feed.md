---
area: console-api
accepted: by 2026-06-01
---
# ADR #95 — Unified Run/Activity feed — Backfills as first-class rows in `/api/runs`

#### Status

Accepted (v0.86.0). Supersedes the Run-redesign discovery
(`docs/redesign/run/DISCOVERY.md`): the unified "Run" object that discovery
proposed is not built as a write-side parent record — Backfill already is that
parent object for the only case that needs one. "Run" is instead realized as a
read-only projection that merges the two write shapes (bare executions +
Backfill records) into one feed. Read-side change only: no DDB schema, no SFN,
no write path.

#### Context

The system has two write shapes that both "produce executions":

- A **single run** (manual trigger, asset-triggered) produces exactly one SFN
  execution. It carries no group-level state, so it needs no parent object —
  the execution *is* the run.
- A **Backfill** (ADR #51/#56/#88) produces N executions over a partition range
  and carries group-level state (progress counts, `max_parallel`, tier gating,
  cancel-all, retry-failed-subset) that cannot live on any single child
  execution. That state needs a first-class parent record — which Backfill has
  (`pipeline-tokens`, `record_type='backfill'`, sentinel
  `_polyris_bulk_backfill`, GSI `backfill-id-index`).

Discovery proposed promoting *every* run (manual/scheduled/backfill) to a
unified write-side `Run` record (a "Run of 1" for the single case). That work
was overtaken: ADR #51–#94 shipped the parent-object machinery scoped to
Backfill, scheduled runs were ruled out of scope (discovery's own note), and
`run_id` was freed by renaming the old task-grouping field to
`parent_execution_id`. What stayed un-built — a `Run`-of-1 wrapper around single
executions — adds a DDB record, an ID, and a GSI query for zero new capability.
The only real rough edge left was read-side: `/api/runs` returned executions
only and excluded Backfills (`should_skip_token_row`), while Backfills lived in
a separate `/api/backfills`, so there was no single "what happened" feed.

#### Decision

**Boundary rule (the durable principle): a parent object exists if and only if
there is group-level state that cannot be reconstructed from any single
execution.** Backfill clears it; a single run does not. Recorded so the
write-side split is intentional-on-record and not re-litigated. No `Run`-of-1.

**Read-side unification — approach A (promote to the backend, not the UI).**
`get_all_runs` (`GET /api/runs`) merges Backfills as first-class rows:

1. **`kind` discriminator in the contract.** Every row carries
   `kind: 'execution' | 'backfill'`. Execution rows are unchanged plus the tag
   (additive, backward-compatible). Backfill rows are built from
   `backfills_repo.list_recent()`.
   - *Why backend, not a UI-side merge of two endpoints:* "Run" now has a named
     definition (this ADR). A core read concept defined in one client's
     TypeScript is the duplication #1 forbids — any second consumer (CLI, API
     client) would re-derive it. ADR #22 (UI reads DDB, backend assembles the
     list) and the project's SSoT discipline put the canonical shape on the
     server. Correct time-ordered pagination also needs one sorted source, not
     a client merge of two capped lists.

2. **Status stays per-kind — no normalization.** Execution rows keep the
   aggregated execution vocabulary (`running`/`succeeded`/`failed`/`aborted`);
   Backfill rows keep the 6-state Backfill vocabulary (ADR #56:
   `pending`/`running`/`completed`/`failed`/`partial`/`canceled`). `?status=`
   matches literally against whichever vocabulary a row uses; `kind` is exactly
   what lets a consumer interpret a status correctly. This is the concrete
   reason `kind` must be in the contract rather than inferred client-side.

3. **`?date=` includes a Backfill when the date is within its partition range.**
   Membership is a string range over the record's `partition_keys`
   (`lo <= date <= hi`). Exact for daily granularity (`YYYY-MM-DD` sorts as
   dates); for weekly/monthly keys a daily `?date=` is best-effort and may not
   match — `?date=` is a daily-oriented filter. Acceptable: the unfiltered feed
   and `?pipeline=`/`?status=` are the primary paths.

4. **`?pipeline=` filters a Backfill on its `target_pipeline`.** A
   cross-pipeline tiered Backfill (ADR #90/#92) surfaces under its target only,
   not under every pipeline in its tier plan. Simple and predictable.

5. **14-day window alignment.** Execution rows default to the 14-day SLA window;
   Backfills come from `list_recent()` (not date-windowed), then the merged list
   is truncated by `limit`. A Backfill older than 14 days can therefore appear
   when its executions no longer do — accepted; the feed is "recent activity",
   and a long-running Backfill over old partitions is exactly what an operator
   still wants to see.

**Children are not embedded.** A feed row expands on demand via existing
endpoints — an execution via `GET /api/execution-children`, a Backfill via
`GET /api/backfills/by-id` — so the list stays cheap and reuses what exists
(#12). The dedicated `/api/backfills` endpoint and the backfills page remain as
a backfill-only filtered view; nothing is removed.

#### Consequences

- `/api/runs` response gains `kind` on every row and may contain `backfill`
  rows; the Runs page becomes the unified Activity feed. Internal-only today, so
  the additive contract change is safe; documented in `docs/operations/API.md`.
- `get_all_runs` now rides `backfills_repo.list_recent()`, a sentinel scan
  (noted since v0.78). At scale the `started_at` GSI optimization already
  backlogged for `/api/backfills` covers the unified feed too — no new cost path
  beyond that shared scan.
- The write-side split is now load-bearing and on record; a future
  "everything is a Run" change must revisit this ADR, not assume it.

#### CLAUDE.md alignment

- **#1 / #12 (no duplication / maximize reuse):** one canonical Run shape on the
  server, not re-derived per client; expand reuses existing children endpoints;
  the backfills page/endpoint are reused, not forked.
- **#2 (follow existing patterns):** DAL repos (`executions_repo`,
  `backfills_repo`), `cors_response`, `(ClientError, BotoCoreError)` + `log.error`,
  the existing row-shaping/sort/limit flow.
- **#6 / #7 (align before implement / ADR for API+architecture):** this entry
  precedes code; the four filter behaviors and the boundary rule are decided
  here, not silently in the handler.
- **#22 (UI reads DDB; backend assembles):** the merge is server-side; the UI
  consumes one assembled list.
