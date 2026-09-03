# CLAUDE.md — UI (React / Next.js)

Read this alongside the root `CLAUDE.md`. Root principles apply everywhere;
this file captures UI-specific rules derived from real bugs found in this codebase.

---

## Status constants are for orchestration, not display

`TASK_SUCCESS_STATUSES`, `TERMINAL_STATUSES`, and similar arrays in
`src/generated/enums.ts` encode **orchestration semantics** — which statuses
allow downstream tasks to proceed, which statuses are "done" for trigger-rule
evaluation. They are generated from the Python SDK constants and must not drift.

**Never use them for UI display counting or labelling.**

```ts
// WRONG — counts skipped as success, misleading the user
const success = tasks.filter(t => TASK_SUCCESS_STATUSES.includes(t.status)).length;

// RIGHT — explicit, unambiguous
const success = tasks.filter(t => t.status === 'success' || t.status === 'succeeded').length;
const skipped = tasks.filter(t => t.status === 'skipped').length;
```

**Why:** `TASK_SUCCESS_STATUSES` includes `'skipped'` because skipped tasks do
not block downstream execution — that is orchestration semantics. But "1 success
+ 2 skipped" is not "3 successes" from the user's perspective. Conflating the
two caused the DAG stats panel to show inflated success counts (fixed in the
skipped-counter PR).

**Where orchestration constants are allowed:** trigger rule evaluation,
`failed` filter logic (`TASK_SETTLED_STATUSES.includes() && !TASK_SUCCESS_STATUSES.includes()`),
any code that decides "is this task done enough to unblock something".

**Where they are forbidden:** counters, badges, stat panels, progress bars,
any user-visible number or label that represents a specific status category.

---

## Dead computed values are bugs waiting to happen

If a `useMemo` or derived value is never used in the render tree, remove it
immediately — don't leave it "for future use". Dead computed values:

1. Accumulate stale logic that diverges from intent over time
2. May carry the wrong semantics (e.g. `done` that counted skipped-as-success
   was dead code in `PipelineDetail.tsx` — but it was still wrong and would
   have caused a bug the moment someone wired it up)

Before adding a computed field to a `stats` object, verify it is actually
rendered. After removing a feature, verify no orphaned computations remain.
