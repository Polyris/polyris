---
area: console-api
accepted: by 2026-11-17
---
# ADR #136 — The pipelines list says what its statuses rest on, and groups only what a DAG names

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §5 (1.3.0).** Decisions 3 and 5 are superseded: the list reads the run rows of the last 14 start days from `run-start-index`, each run's status and `waiting_on` from its run row's summary, and asks Step Functions nothing about a run; the engine keeps the run row true. `stats_incomplete` now means a start day that could not be read, and `index_building` says that DynamoDB is still building the index after an upgrade. A running latest run carries the count of its failed tasks (`failed_tasks`), and such a pipeline is pinned under **Needs attention** too. The API is otherwise unchanged.

**Context.** The console's pipelines sidebar was redesigned in 1.2.1: one line per pipeline, the latest run's status, the last 10 runs, and a **Needs attention** section on top. Four things in the old list stood in the way.
- `/api/pipelines` computed `today_stats` and `progress` for one day, chosen by its `date` parameter. The new sidebar reads neither: it shows runs, not a day's task counts.
- The sidebar made a group from the part of a name before the first `-`. `acme-orders` and `acme-billing` became `acme` whether or not the user meant it, and `daily-report` became `daily`.
- A run whose tasks say `running` stayed `running` on the list after its Step Functions execution died. History already checked such runs (ADR #112 §7); the list did not, so a dead run never reached **Needs attention**.
- A failed read looked like an answer. A registry the API could not read was replaced by a scan of the tokens table, which has no groups or schedules and misses every pipeline without a recent run; when that failed too, the answer was an empty 200, and the sidebar read "No pipelines registered yet". A date the API could not read left that day's runs out of the statuses without a word.

**Decision.**

1. **Groups come only from `DAG(group=)`**, compared after trimming and without case. A pipeline without `group=` is listed under **Ungrouped**, or in a flat list when no pipeline sets one. The console infers no group from a name.
2. **`/api/pipelines` answers `{"pipelines": [...], "stats_incomplete": bool}`.** Each pipeline carries its latest status, `recent_runs` (newest first, the last 14 days), its SLA and `waiting_on` (`waiting_decision`, `waiting_paused` or `null`). `today_stats`, `progress` and the `date` parameter are gone. A registry that cannot be read fails the request; the tokens table is scanned only when the registry is empty, and a failed scan fails the request too.
3. **The latest run is checked against Step Functions when its tasks say `running`**, with the same helper as `/runs` (`check_with_sfn`, ADR #112 §7). Only the latest: the status and **Needs attention** read it, and older runs in the sparkline keep their derived status.
4. **`stats_incomplete` is `true` whenever a read the statuses rest on did not happen**: a date that could not be read, or a check that failed, came after the deadline, or had no state machine to ask. A call the console API is not allowed to make (`constants.ACCESS_DENIED_CODES`) fails the request instead, here and in `/runs` (CLAUDE.md, *Error Visibility*). A KMS denial is not among them (its fix is a key policy): it marks the stats incomplete, and the log names it.
5. **Checks start only in the first 12 s of the request** (`RECONCILE_DEADLINE_SECONDS`). A Step Functions call that starts at the deadline gives up within 13 s (`config.SFN_CLIENT_CONFIG`: two attempts of 2 + 4 s, a backoff under 1 s), which leaves 4 s of API Gateway's 29 s for a cold start and the answer (Principles #52, #53). The bound covers the Step Functions call only: the read of a failed run's tasks that follows it uses the console's DynamoDB client, whose timeouts this ADR does not change.
6. **The console says a failed read where the reader looks**, never as an empty list (`ui/CLAUDE.md`). The sidebar keeps its last list and says from when its statuses are; a failed first load shows the error and a retry. That the API cannot be read at all is said once, by the **System** status in the header: amber "no connection" or "API error".

**Key design choices.**

*No group inference.* A guess that is right for one naming scheme is wrong for the next, and a wrong group is worse than none: it hides a pipeline where the user does not look for it. `group=` already existed in the DSL and costs one argument.

*Remove the day fields rather than keep them unread.* No caller in this repository reads them, and they cost a second pass over every row. A client that read them takes the same facts from `status` and `recent_runs`, or a day's tasks from `/api/tasks?date=`; the CHANGELOG says so.

*No cache of the Step Functions checks.* The console polls the list every 30 s, so a cache that keeps a running run's status for less than that is never hit by the same console, and one that keeps it longer shows a dead run as running for longer. At one `DescribeExecution` per running pipeline per poll, 60 pipelines running at once in 5 open consoles make 10 calls a second, the smallest refill rate AWS documents for that call; other console routes and the sweeper draw on the same bucket. Past that, calls are throttled, the check fails, and `stats_incomplete` says so: the list is late, never wrong. A cache is worth adding when a deployment shows throttling, not before (Principle #18).

*A late or failed check is said, not hidden.* Keeping the derived status is the only honest fallback, but on its own it shows a dead run as running. `stats_incomplete` lets the sidebar say that a status may be out of date. An execution Step Functions no longer knows (`ExecutionDoesNotExist`, after a state machine was replaced) is such a failure too: the note stays until a newer run of the pipeline is its latest, which is true, since nothing can confirm that run.

*A failed registry read fails the request.* The scan that stood in for it gave a list that looked whole: no groups, no schedules, no pipeline without a recent run. A 500 reaches the sidebar as an error with a retry, and the **System** status says why.

*A patch release.* The console and its API ship together, and the fields go in 1.2.1 by the maintainer's decision; the CHANGELOG's *Upgrading to 1.2.1* tells a script what to read instead.

**Consequences.**
- Code: `routes/pipelines_list.py` (`list_pipelines`, `_finish_runs`, `check_with_sfn`, `RECONCILE_DEADLINE_SECONDS`); the console's `utils/pipelineSidebar.ts`, `PipelinesSidebar.tsx`, `SystemStatus.tsx` and `useSystemStatusQuery.ts`.
- Tests: `console_api/tests/routes/test_pipelines_list_reconcile.py` checks against a moto Step Functions backend: a dead run, a live one, a failed, throttled, denied or late check, a missing state machine, a recovered run in the SLA, a registry or tokens table that cannot be read. `test_reconcile_sfn_status.py` raises a denial for `/runs`. `PipelinesSidebar.test.tsx` covers the failed load and the stale list.
- Docs: `docs/operations/API.md`, `docs/operations/UI.md`, `docs/features/DSL.md` (`group=`), the CHANGELOG's *Upgrading to 1.2.1*.
