---
area: console-api
accepted: by 2026-08-17
---
# ADR #108 — A pipeline's runs are read through an inverted GSI; the cross-pipeline feed keeps its per-date fan-out

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §5 (1.3.0).** The Runs tab, without a filter or with a pipeline filter, reads run rows from `run-start-index`: the start days of the last 90 days, newest first, until its page is full, each run's status from its run row's summary. A date filter still reads the task rows of its date. The Tasks tab keeps the reads below.

**Context.** Runs are derived, not stored: task rows are grouped by `pipeline_execution`
and the run status comes from `derive_execution_status()` (ADR #112). The only index into
them was `date-pipeline-index` (**HASH `date`**, RANGE `pipeline_name`), which answers
"who ran on day D". Answering the question the UI actually asks — *"this pipeline's runs,
newest first"* — therefore required looping a day at a time, and every caller had to pick
how many days to loop. That produced four dialects of one workaround (`/runs` hardcoded
`sla_days = 14`; `/pipeline-executions` a caller-supplied range; the asset matrix 14/60
**parallel**; the calendar a month) plus a hidden `executions[:50]` cap, and it capped
visible history at an arbitrary window rather than at the data's own lifetime. It also
forced two UI workarounds: a "No executions for {date}" dead end and a "Latest" escape
hatch, both artefacts of the drawer being scoped to a single date.

**Decision.** Add `pipeline-date-index` — the **inverse** of the existing index
(**HASH `pipeline_name`, RANGE `date`**, projection INCLUDE). Both key attributes are
already written on every task row by the wrapper, so this needs **no write-path change,
no new Step Functions state, and no migration** — DynamoDB backfills the index from
existing rows, and old runs are visible immediately. `ExecutionsRepo.query_runs_by_pipeline`
reads it newest-first and pages with an opaque date cursor, cutting only on a date
boundary so an execution's task set is never split across pages.

**The cross-pipeline feed (`/runs` with no pipeline filter) deliberately keeps its
per-date fan-out.** There is nothing to hash on for "everything, newest first"; a
constant partition key (`feed_pk = "RUN"`) would funnel every write into one DynamoDB
partition — the hot-partition antipattern — and would need a new attribute that only new
rows carry, blanking the feed until the old rows age out. **`date` is the natural shard
key for a time-ordered feed**, so the loop there is correct design, not debt. What was
removed is the *dialects*: the window now comes from `Limits.SLA_DAYS` and the worker cap
from `Limits.PARALLEL_DATE_QUERIES` (previously duplicated verbatim in EE `drift.py` and
`matrix.py`).

**Consequences.**
- Depth is governed by the row TTL alone (~31 days today, per the wrapper's `ttl`). Raising
  the TTL deepens visible history **with no code change** — the point of the exercise.
- One extra GSI write per task-row write (PAY_PER_REQUEST) plus INCLUDE-projection storage;
  ~\$0.45/month at ~60k executions/month. No capacity planning.
- The history drawer lists every run it can see, so "Latest" is gone (click the top entry).
  The page still scopes the DAG to a date, so "No executions for {date}" / "View latest run"
  remain — but they are no longer a dead end, since the drawer reaches any run regardless.
- Two access patterns remain, one per question, both behind the DAL. That is deliberate:
  forcing them into one would mean the hot-partition antipattern above.
