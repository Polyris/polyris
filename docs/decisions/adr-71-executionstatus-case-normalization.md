---
area: sdk
accepted: 2026-05-27
---
# ADR #71 — ExecutionStatus case normalization

#### Context

`ExecutionStatus` (pipeline-execution status) was a mess in the frontend
type: `'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'TIMED_OUT' | 'ABORTED' |
'running' | 'succeeded' | 'success' | 'failed' | 'timed_out' | 'aborted' |
'stopped'` — twelve variants for what should be five+stopped statuses.

Root cause: AWS Step Functions `DescribeExecution` API returns status
in UPPERCASE (`'RUNNING'`, `'SUCCEEDED'`, ...). Internal Python code
and DDB storage use lowercase. Without a single normalization point
at the boundary, raw SFN values leak through certain code paths into
API responses, forcing the frontend type to accept both cases.
Additionally, some legacy code wrote `'success'` (the `TaskStatus`
form) instead of `'succeeded'` (the `ExecutionStatus` form), adding
yet another variant.

Two duplicate normalization maps existed:
- `SFN_STATUS_MAP` in `console_api/constants.py` (SFN → TaskStatus)
- A local `sfn_status_map` in `pipelines_list.py` (SFN → ExecutionStatus,
  collapsing `TIMED_OUT` → `'failed'`)

This ADR is Stage 2 of the multi-stage alignment plan (after ADR #70).

#### Decision

**1. One canonical helper** — `normalize_execution_status(status, log_warn=None)`
in `sam/lambdas/_shared/constants.py` (mirrored into
`console_api/constants.py` via `make sync-constants`).

```python
EXECUTION_STATUS_CANONICAL = {
    'running', 'succeeded', 'failed', 'timed_out', 'aborted', 'stopped',
}
```

Maps UPPERCASE SFN values, legacy `'success'`/`'SUCCESS'`, and returns
already-canonical values unchanged. Idempotent. Optional `log_warn`
callable for diagnostic warnings on unexpected values (CLAUDE.md hard
rule #4).

**2. Apply at boundary** (per Q2 = read + write):
- Replaced local `sfn_status_map` in `pipelines_list.py:_reconcile_running`
  with the helper. Removes the duplicate.
- Behavior change: `TIMED_OUT` previously collapsed to `'failed'`; now
  preserved as `'timed_out'`. Downstream "SFN-failed-but-tasks-resolved"
  recovery check updated from `if sfn_status == 'failed'` to
  `if sfn_status in {'failed', 'timed_out'}` to keep semantics.

**3. Narrow frontend type** —
```ts
export type ExecutionStatus =
  | 'running' | 'succeeded' | 'failed' | 'timed_out' | 'aborted' | 'stopped';
```

Removed UPPERCASE variants and the legacy `'success'`. TypeScript now
catches at compile time any code that still compares against UPPERCASE.

**4. Frontend defensive code updated**:
- `CalendarView.tsx` — removed UPPERCASE branches in the status→bucket
  mapping. Added `'timed_out'` → `'failed'` visual bucket.
- `asset-tabs/TabEvents.tsx` — kept accepting both forms because
  `evt.metadata.status` is free-form metadata (not ExecutionStatus
  itself, set by various SFN templates that haven't been migrated to
  canonical writes yet).

**5. Tests** — `tests/test_normalize_execution_status.py`:
18 tests covering UPPERCASE → canonical mapping, idempotence on each
canonical value, `'success'`/`'SUCCESS'` legacy mapping, `None` input,
unknown value handling (log + return original), and a lock test on
the canonical set so accidental drift is caught.

#### What this does NOT do

- **Does not migrate `SFN_STATUS_MAP`** (the SFN → TaskStatus mapping).
  Different domain — TaskStatus is for tasks within an execution,
  with values like `'waiting'`, `'deps_ready'`, etc. that don't exist
  in ExecutionStatus. Stage 4 (SSoT enums codegen, ADR TBD for v0.79.0)
  will deduplicate that map across files.
- **Does not normalize `evt.metadata.status` in asset events**.
  Free-form metadata, set by various SFN templates. Migrating those
  templates to write canonical values is a separate cleanup.
- **Does not collapse `'success'` vs `'succeeded'`** anywhere except
  ExecutionStatus normalization. Two statuses exist by design at
  different layers (TaskStatus uses `'success'`; ExecutionStatus uses
  `'succeeded'`). The normalizer only canonicalizes ExecutionStatus
  inputs that historically conflated them.

#### Trade-offs

- **Defense-in-depth normalization (read + write)** has a small CPU
  cost (one dict lookup per status). Negligible compared to DDB I/O.
- **Behavior change on `TIMED_OUT`**: was hidden inside `'failed'`,
  now surfaces as distinct. UI's "Failed" filter still includes both
  via `CalendarView` mapping; the new precision matters if anything
  downstream specifically wants to distinguish (alerts, retry logic).

#### Lesson

When a type union grows to 12 variants for what should be 5, the type
isn't doing its job — it's documenting historical confusion. The fix
isn't more variants; it's enforcing a contract at the boundary and
narrowing the type to match. TypeScript then becomes the enforcement
mechanism — any code still expecting UPPERCASE breaks at compile time,
which is exactly when you want to know.
