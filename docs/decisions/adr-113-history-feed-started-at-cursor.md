---
area: console-api
accepted: by 2026-08-17
---
# ADR #113 — The History feed pages on a `started_at` cursor; `next` is the only end-of-feed signal

**Context.** ADR #108 removed the *window* dialects from the runs feed but left the
**cap** untouched: `get_all_runs` ended in `all_runs = all_runs[:limit]`, unconditionally,
on every path. `/api/tasks` did the same with `items[:limit]`, on top of a full-table Scan
whose row order is arbitrary — so "the newest 100 tasks" was the newest 100 of whichever
10,000 rows the Scan happened to read first. The UI then rendered the survivors under the
label `50 runs`, which is not a count of anything: it is the page size wearing a count's
clothes. Nothing in the response distinguished "there are exactly 50 runs" from "there are
5,000 and you may have the first 50", so the UI could not have told the truth even if it
had wanted to, and there was no way to ask for the 51st.

**Decision.** Both feeds page on an opaque **`before` cursor carrying a `started_at`**
("give me what is older than T") and answer with **`next`** — the last returned row's
`started_at`, or `null` when nothing older exists. `null` is the point: a full page is not
an end-of-feed signal, so without an explicit one the UI must either lie or guess.

`started_at` rather than a DynamoDB key, because the runs feed merges two sources with no
common key — executions and Backfills (ADR #95) — whose only shared ordering attribute is
`started_at`, which is also what the feed already sorts by. One timestamp pages the merged
list; a key-based cursor would have to encode both sources, and a composite cursor would
buy nothing the sort order doesn't already give.

The **cursor is a filter, not a scope**: it says which rows are eligible, never where they
come from. Sources stay exactly as ADR #108, runs feed left them, and both routes now share all three:
- explicit `date` → one indexed query (already bounded);
- `pipeline`, no date → `pipeline-date-index`, no window (bounded by the row TTL);
- neither → the `Limits.SLA_DAYS` fan-out over `date`, **started at the cursor's date**
  rather than at today. `date` remains the natural shard key for a cross-pipeline time
  series (ADR #108, runs feed) — the walk is design, not debt, and paging deeper now costs *fewer*
  queries, not more.

Starting the walk at the cursor's date is safe because a row's `started_at` is never
earlier than its logical `date`: no row older than T can hide on a date newer than T's.

**Consequences.**
- `/api/tasks` loses its Scan. It was the more expensive read *and* the less honest one —
  unbounded cost, arbitrary order, and no cursor could have paged it truthfully. Its
  pipeline-filtered path now reads `pipeline-date-index`, so both halves of History reach
  equally far back; its unfiltered path inherits the same SLA window the runs half has
  always had, in exchange for a bounded, ordered read.
- `backfills_repo.list_recent` grew `before`, applied **before** its `limit` slice. After
  it, the newest `limit` records are all that survive to be filtered — which is exactly the
  set any cursor has already served — so Backfills would silently drop out of the feed on
  page two. The cursor filter stays in memory: DynamoDB applies `FilterExpression` after
  reading, so pushing it down saves no capacity, and it would drop records with no
  `started_at` instead of sorting them last, where the rest of the feed puts them.
- **Every bounded source reads one row past the page.** `next` is inferred from having more
  rows than fit, so a source that returns exactly `limit` makes a full page look identical
  to an exhausted feed — reporting end-of-data with rows still behind it, which is the same
  silent truncation this ADR exists to remove, merely moved one layer down. Sources that
  read a whole window or date get the surplus for free; the two bounded ones must ask for
  it: `feed.pipeline_rows_before` requests `want + 1` and stops on `count_fn(rows) > want`
  (strictly), and `_build_backfill_run_rows` asks `list_recent` for `limit + 1`. Landing on
  exactly `limit` is then only reachable by exhausting the source, which is the one case
  where it is true.
- The index read needs a **fill loop** (`feed.pipeline_rows_before`). Its repo cursor is a
  *date*, so on a busy cut date one read returns only rows the cursor already served; a
  single read would hand back a short page and, worse, never advance past that date —
  paging would stall with older runs still behind it. The loop pulls whole dates until it
  overshoots the page or the index runs out. It costs nothing on the first page, where the
  first read is always enough. (The stall needs the repo to hand back a date cursor, which
  needs a >1MB query page — real for a busy pipeline, and the same cursor the history
  drawer already pages on, but not reproducible under `moto`, so it is pinned by a unit
  test against the repo's documented contract rather than end-to-end.)
- The count is now honest, and says so: `50+ runs` while `next` is non-null, `73 runs` once
  it isn't. Two paginations coexist in the footer and are not the same thing — the pager
  walks loaded rows, `Show older` extends the feed.
- Rows without a `started_at` sort last and cannot be a cursor, so a page ending on one
  stops paging. Every task row is stamped at registration and every Backfill at start; this
  is a broken-data guard, not a supported mode.
- **Nothing is ever cut inside a unit, at any layer.** This turned out to be the whole of
  it, and the review found two places that were: the day read cut inside a *pipeline*, and
  the page cut inside an *instant*. The rule is now uniform —
  `query_runs_by_pipeline` cuts on whole dates, `query_runs_by_date` on whole pipelines,
  `page_by_started_at` on whole `started_at` values.
  - The day read was the serious one. `date-pipeline-index` is ordered by `pipeline_name`,
    so a row-count cut lands mid-pipeline — and a run's status is *derived* from the rows
    the caller got (ADR #112), so a split run is not a missing run, it is a **wrong** one:
    a failed run whose failing task fell past the cut renders `success`. Measured at three
    hourly 8-task pipelines (576 rows on one day, cap 500): 23 of 72 failed runs green.
    `ExecutionsRepo.query_runs_by_date` reads whole pipelines and drops the one it stopped
    inside, keeping the read while only one pipeline is buffered so a single busy pipeline
    is never the one dropped. The budgets are therefore floors, not caps
    (`*_MIN_ROWS_*`), and a date that fits one DynamoDB page comes back whole — which
    costs more Lambda memory than the old truncation and is the right trade: the cap was
    never saving capacity anyway (DynamoDB had already returned the page; the truncation
    only threw it away).
  - The page cut was the subtle one. The cursor is a strict `<`, so a row sharing the
    boundary's `started_at` is filtered out by the very cursor meant to fetch it. The page
    absorbs its twins instead. This is what makes a plain timestamp sufficient rather than
    a compromise: a composite cursor would only buy back what the boundary already gives.
- What remains, honestly: on a day too big for one DynamoDB page, the pipelines past the cut
  are still missing from the *unfiltered* feed (filter to a pipeline and they are all there,
  windowless). That is a completeness gap, not a correctness one, and closing it needs a
  per-day cursor over `pipeline_name` — which conflicts with ordering the feed by
  `started_at`. Deliberately not answered here. `query_all` already logs `Hit max_items
  limit`, and `query_runs_by_date` cuts on the same condition, so whether this is live is
  measurable rather than arguable.
- **1.3 update.** The Tasks tab no longer reads task rows by date or by pipeline: it reads the run rows
  the Runs tab reads (`run-start-index`, ADR #137 §5), then the task rows of the runs on its page, and pages on the
  start of its last run. `query_runs_by_pipeline` and its fill loop are gone. Reading a pipeline's task rows by date,
  a page of a pipeline of 2,000 tasks read 101 runs, about 202,000 rows; a rare status read its whole history,
  kept a year and 90 days in 1.3; and the cursor, cut to a day, lost every hourly, weekly and monthly run (TR-7
  estimate, P-6; TR-10, B-1 and D-1). A date filter still reads one date's task rows, for every run date that
  holds the day (`feed.day_keys`).
