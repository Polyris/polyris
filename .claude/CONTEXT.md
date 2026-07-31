# CONTEXT.md — vocabulary

Pure vocabulary. No implementation detail, no design, no rationale — those live
in `CLAUDE.md` and the ADRs. If a term needs a paragraph, it needs an ADR.

This file exists because several terms here are near-synonyms that are **not**
interchangeable, and confusing them has produced real bugs. Every term below is
pinned by `tests/sdk/test_context_terms.py`, which fails if a term stops
appearing in the code — a glossary nobody can trust is worse than none.

Add a term the moment it is resolved, not in a batch at the end.

---

## Execution identity

| Term | Means |
|---|---|
| **pipeline** | A DAG, as authored. Identified by `dag_id`. |
| **run** | One dated invocation of a pipeline. What the UI's Runs feed lists. |
| **execution** | A Step Functions execution. Every task has one; a run has several. |
| **execution_name** | DDB partition key for a task row. `{task}-{date}-{short}`. |
| **pipeline_execution** | Full SFN execution ARN of the parent run. |
| **pipeline_execution_short** | Last 20 chars of `pipeline_execution`, `.` and `:` stripped. Format `-{date}-{hex8}`. Kept deliberately (ADR-tracked); ~103 references. |
| **dependency_key** | `{task_name}-{pipeline_execution_short}`. Must match exactly between the registration writer and the `query_subscriptions` reader. |
| **wrapper_execution_arn** | The `dependency_wrapper` execution for one task, distinct from the task's own service call. |

**The trap:** `run` and `execution` are not the same noun. One run contains many
executions. A feed that says "runs" and queries executions counts wrong.

## Scheduling and time

| Term | Means |
|---|---|
| **date** | The logical partition date (`YYYY-MM-DD`), not wall-clock time. |
| **partition** | One unit of work for one date at the pipeline's granularity. |
| **granularity** | Cadence inferred from the cron: how a date range expands into partitions. |
| **backfill** | A deliberate re-run across a range of past partitions, tracked as its own object. |
| **re-run** | Running a single existing run again. Not a backfill; no backfill record. |
| **catchup** | Not a polyris concept. |

**The trap:** a date range is not a partition count until granularity is applied.

## Assets

| Term | Means |
|---|---|
| **asset** | A declared piece of data a task produces or consumes. |
| **outlet** | An asset a task produces. |
| **inlet** | An asset a task declares as an input. |
| **wait_for** | Pull-based dependency on an asset, with optional freshness/consecutive checks. |
| **freshness_hours** | Maximum age of an asset event for it to count. May be fractional. |
| **asset event** | A record that an asset was produced for a date. |

## Editions

| Term | Means |
|---|---|
| **CE** | The public repo, `polyris`. The whole free product. |
| **EE** | The private repo, `polyris-ee`. The paid `ee/` overlay only. |
| **free** | Ships in CE. Authoring, basic read, running, live-run intervention. |
| **Team / Enterprise** | Paid tiers. Team is the lower one; unsure between them → Team. |
| **paid surface** | The `PaidSurface` slots the free UI reaches only via `@/ee-active.generated`. |
| **OSS build** | A build with `src/ee/` absent. What `check-oss-build.sh` proves still works. |

**The trap:** "not in the OSS build" and "not implemented" look identical from
CE. Check `CONTRIBUTING.md` (Common Tasks section) before concluding a feature is missing.

## Testing

| Term | Means |
|---|---|
| **measured core** | The modules coverage actually measures — everything not in the `omit` list. The 100% floor is over this, not the whole tree. |
| **gate** | A command that must exit 0 before work is done. Listed in `make check`. |
| **guard test** | A test that asserts a rule about the tree itself, not a behaviour. |
| **drift** | A generated artifact, doc claim, or mirrored constant that no longer matches its source. |
| **blast radius** | Every consumer of every symbol a change touched, across both repos.
| **mutation test** | Re-introducing a bug to prove the new test catches it. |
