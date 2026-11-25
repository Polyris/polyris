# ADR #93 — UI status vocabulary consolidated onto generated enums (single source)

#### Status

Accepted. Chore (v0.84.1). Closes the last ungated enum-duplication surface
found in the v0.84.0 whole-repo consistency audit. No product behavior change.

#### Context

The whole-repo audit found `ui/src/utils/constants.ts` hand-maintained a
`TASK_STATUS` value object (and a `TERMINAL_STATUSES` set) parallel to the
generated, gated `ui/src/generated/enums.ts`. It was in sync with canonical at
the time, but nothing pinned it — a silent-drift risk and the UI tail of the
enum-SSoT consolidation (the backend had already been consolidated:
`console_api/constants.py` re-exports from `constants_generated.py`, pinned by
`test_enum_drift`). The duplication existed only because the codegen emitted a
type union (`type TaskStatus`) and terminal-status arrays, but not an
ergonomic named-value object (`TASK_STATUS.SUCCESS`) that UI components need.

#### Decision

Eliminate the copy rather than police it with a second test (a drift test
would institutionalize the duplication; CLAUDE.md #2/#12 say remove it).

- `sync_enums.py` additionally emits a named-key const object
  (`export const TASK_STATUS = { SUCCESS: 'success', ... } as const`) from the
  canonical `TaskStatus` Enum members, alongside the existing union type.
- `ui/src/utils/constants.ts` stops defining `TASK_STATUS`; it imports and
  re-exports it from `@/generated/enums` (mirroring the backend re-export
  pattern), and rebuilds `TERMINAL_STATUSES` from the generated
  `TASK_TERMINAL_STATUSES` array. Derived UI groupings (SUCCESS/FAILURE/
  WAITING/ACTIVE/COUNTDOWN_STATUSES) and presentation constants (STATUS_COLORS,
  normalizeStatus, VIEWS, MS) stay — they reference the single generated source
  and are typed `Set<string>` since the generated values are now `as const`.
- Consumers are unchanged (they still import `TASK_STATUS` from
  `utils/constants`, now a re-export), so the migration is zero-churn at the
  call sites.

`check-generate-enums` now covers the UI value object (it is generated), so the
gate falls out for free — no new test needed.

#### Consequences

- One generated, gated source for UI status values; the silent-drift surface
  is closed without adding a parallel test.
- Only `TaskStatus` gets a named-value object emitted — it is the only family
  with UI value-access consumers (#1: no dead generated exports). Other
  families can get the same treatment if/when a consumer appears.
