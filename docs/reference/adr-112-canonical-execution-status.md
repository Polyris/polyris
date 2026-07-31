# ADR #112 — Canonical execution-status derivation and reconciliation

> **Status:** ACCEPTED — implementation in progress. Establishes a single canonical
> way to derive a pipeline-execution's status from its task statuses, a single
> reconciliation policy against Step Functions, and one status vocabulary shared by
> every surface (History `/runs`, the execution-history dropdown `/pipeline-executions`,
> the sidebar, and SLA). No IAM changes. Supersedes the *value choice* (not the
> mechanism) of ADR #71 — see "Supersedes" below.

## Context

A pipeline-execution's status is **derived** from the statuses of its constituent
tasks (DynamoDB is the system of record, ADR #97 / DESIGN_DECISIONS #22; SFN is
consulted only to reconcile executions that still look `running`). That derivation
was re-implemented by hand in three places that drifted apart:

- `get_all_runs` (`routes/executions.py`) — the `/runs` feed (History page). Knows
  `stopped_statuses = {stopped, aborted, upstream_failed}` and maps them to `aborted`.
  Does **not** reconcile with SFN.
- `_aggregate_executions` (`routes/pipelines_list.py`) — the `/pipeline-executions`
  list (the execution-history dropdown). Its terminal set is only
  `{success, succeeded, skipped, failed}`; it has **no** notion of stopped/aborted, so a
  stopped run falls through to `running` (then SFN-reconciled) or `failed` (if any task
  failed). It **does** reconcile `running` with SFN via `_reconcile_running`.
- The SLA / progress block in `list_pipelines` (`routes/pipelines_list.py`) — a third
  grouping that treats `aborted`/`stopped` as failures and even emits `success` (not
  `succeeded`) for the "success" bucket.

Symptom that surfaced this: **the same stopped execution shows `aborted` on the
History page but `failed` in the execution-history dropdown.** The History value is
correct; the dropdown is wrong. Root cause is the duplicated, drifted derivation —
not a display bug.

Two vocabulary questions were entangled with the derivation:

- `success` vs `succeeded`. Tasks are written with `success`. Execution status was canonicalised to `succeeded`
  (SFN-aligned) by ADR #71, and the frontend carried a `normalizeStatus` band-aid that
  mapped `succeeded → success` for display. Net effect: tasks display `success`, runs
  display `succeeded` — inconsistent.
- `stopped`. It is a **task** status meaning "stopped via UI, restartable" — explicitly
  **non-terminal**. It is written when a task (or its parent execution) is stopped. An
  *execution* is never `stopped`: stopping a run aborts the SFN execution (→ `aborted`)
  and writes `stopped` onto its still-waiting **tasks**. So `stopped` in
  `ExecutionStatus` is vestigial.
- `timed_out` / `recovered` are, by definition, reconciliation-derived: `recovered`
  means "SFN reports failed/timed_out but all tasks resolved". Pure DynamoDB derivation
  cannot produce them. Because `/runs` did not reconcile, it could never show them,
  while the dropdown could — another divergence.

## Decision

1. **One canonical derivation.** `derive_execution_status(task_statuses) -> str` lives in
   `polyris/constants.py` (next to `normalize_execution_status`, so the existing codegen
   picks it up), is codegen-synced into every lambda's `constants_generated.py`, and is
   drift-tested. It produces the DynamoDB-derivable subset: `{running, success, failed,
   aborted}`.

2. **One canonical reconciliation.** `reconcile_execution_status(base, sfn_status,
   all_tasks_resolved) -> str` is the single place that consults SFN for an execution
   that still looks `running`, producing `timed_out` / `recovered` / `aborted` /
   terminal. Both read endpoints use derive-then-reconcile (decision "b" below).

3. **`success` is the one canonical value, system-wide.** Task and execution "success"
   are both `success`. `succeeded` becomes an accepted **input alias only** (SFN
   `SUCCEEDED`, legacy rows) that normalises to `success`. This supersedes ADR #71's
   value choice: `normalize_execution_status` now returns `success` for SFN `SUCCEEDED`
   (was `succeeded`). The frontend `normalizeStatus` band-aid is removed — the backend
   is authoritative.

4. **`stopped` is a task status only.** Removed from `ExecutionStatus` (kept in
   `TaskStatus`). An execution that was stopped derives/reconciles to `aborted`.

5. **Reconciliation policy (b): consistent on both read endpoints.** `/runs` now also
   reconciles its `running` executions with SFN, exactly as the dropdown already does
   (reconcile touches only `running` executions, so terminal rows cost zero SFN calls).
   This eliminates the whole class of divergence — including "stuck running" (a run whose
   last task died without writing a terminal status) and `timed_out`/`recovered`, which
   now appear consistently on both surfaces.

6. **SLA excludes `aborted`.** A user-initiated stop is not a system failure; aborted
   runs are excluded from the SLA denominator, not counted as failures.

7. **One output vocabulary for every surface.** History, dropdown, sidebar, and SLA all
   consume the canonical derivation + reconciliation. No surface keeps its own copy.

## Canonical vocabularies (post-ADR)

- `ExecutionStatus` = `{running, success, failed, timed_out, aborted, recovered}` —
  `stopped` removed, `succeeded` → `success`.
- Derivation (DynamoDB only) yields `{running, success, failed, aborted}`.
- Reconciliation adds `{timed_out, recovered}` (and confirms `aborted`) from SFN.
- `TaskStatus` keeps `stopped` and keeps `success` as canonical (`succeeded` = alias).

## Consequences

- Backend: the three derivations are replaced by the canonical helpers; `_reconcile_running`
  becomes the shared reconciliation; `/runs` gains reconciliation.
- Enums regenerated (lambdas + `ui/src/generated/enums.ts`); `stopped` leaves
  `ExecutionStatus`.
- Frontend: `normalizeStatus` removed; `STATUS_COLORS`, `.task-status-badge`,
  `.sb-pipeline-icon`, and the status filters align to the canonical vocabulary. The
  `/runs` (Runs) filter drops `stopped` and gains `timed_out`/`recovered`; the Tasks
  filter keeps task-level `stopped`.
- EE: `GanttChart` (uses `normalizeStatus`) is updated to the canonical form. Backfill
  derivation (ADR #73) is a separate, already-canonical concern and is untouched.
- Tests: exhaustive derivation/reconciliation unit tests; a **cross-endpoint consistency
  test** (identical task sets ⇒ identical execution status from `/runs` and
  `/pipeline-executions`) that would have caught the original bug; a codegen drift test
  for the new helpers.
- Performance: reconciliation on `/runs` is bounded to `running` executions. If a
  deployment has many concurrent running executions, batch/cache the `describe_execution`
  calls — noted as a later optimisation, not required for correctness.

## Supersedes

ADR #71 (`normalize_execution_status`) — **only its value choice**. The mechanism
(centralise SFN → canonical lowercase at the boundary, codegen-synced, drift-tested)
stands and is extended here. The canonical "success" value changes from `succeeded` to
`success` and to remove the frontend band-aid.
