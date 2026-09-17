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

## Copy-to-clipboard always shows visible text feedback

An icon change alone (Copy → CheckCircle2) is not enough — the user may not notice it.
Every copy button must show a short text label `"Copied!"` that appears next to the
button and disappears automatically after ~1.5 s.

```tsx
// pattern — copiedKey is a string | null useState in the parent component
{copiedKey === 'my_field' && <span className="td-copy-feedback">Copied!</span>}
<Button
    onClick={() => handleCopy('my_field', value)}
    aria-label={copiedKey === 'my_field' ? 'Copied' : 'Copy to clipboard'}
>
    {copiedKey === 'my_field' ? <CheckCircle2 size={14} /> : <Copy size={14} />}
</Button>
```

`HelpModal.tsx` uses icon-only — that predates this rule and should be updated
opportunistically when the file is touched for another reason.

---

## `execution_id` is always the short pipeline execution name, never the full ARN

The backend's DynamoDB `pipeline_execution` field stores only the **short execution
name** (e.g. `hello-world-run-2026-09-03-3bb2ff0c`), never the full SFN ARN
(`arn:aws:states:…:execution:hello-world:hello-world-run-…`).

Whenever code receives a full ARN and needs to store or pass it as an `execution_id`
for subsequent queries, extract the short form with `arn.split(':').pop()` first.
Never use the raw ARN as `execution_id`.

```ts
// WRONG — backend DDB GSI can never match a full ARN
setSelectedExecution({ execution_id: result.execution_arn, … });

// RIGHT — extract short name, which matches DDB pipeline_execution values
const execShort = result.execution_arn.split(':').pop();
setSelectedExecution({ execution_id: execShort, execution_short: execShort, … });
```

**Why:** `usePipelineDetailQuery` passes `selectedExecution.execution_id` directly as the
`pipeline_execution` query param. The backend calls `query_by_pipeline_execution(pipeline_execution)`,
which uses a DDB KeyConditionExpression `Key('pipeline_execution').eq(value)`. DDB stores
the short name — passing the full ARN always returns `tasks: []`, causing the UI to show
the blueprint/Definition state instead of the live execution.

---

## Flex-container children must be a single element, never raw text mixed with JSX

A `display: flex` container renders every child as a flex item. If the children
are `"leading text " + <code>value</code>` (a text node plus an element node), the
container makes two flex items with gap between them — the text and the code
appear with an unexpected space, and vertical alignment breaks. Wrap
text-plus-element children in a `<span>`.

```tsx
// WRONG — text node + <code> node = two flex items with a gap
<div className="td-tab-empty">
    Waiting for upstream <code>{depName}</code> to publish output.
</div>

// RIGHT — one <span> element = one flex item; text flows naturally
<div className="td-tab-empty">
    <span>Waiting for upstream <code>{depName}</code> to publish output.</span>
</div>
```

**Why:** hit in 1.0.0 TaskDetailModal's pending-state cards. `.td-tab-empty`
is `display: flex; align-items: center; gap: 0.5rem;` (icon + message). The
message string had an inline `<code>` for the task name, which the flex layout
turned into a second flex item — visible gap between "upstream" and the code
element, misaligned icon. A `<span>` wrapper makes the whole message a single
child.

**How to apply:** whenever a flex or grid container's child is a *message*
(free text with any embedded element), wrap it in one element. This includes
`display: flex` and `display: grid` — both treat every direct child as an
item. Inline formatting (`<em>`, `<strong>`, `<code>`, `<a>`) inside plain
block containers (`<p>`, `<div>` without flex) is fine and doesn't need the
wrapper.

---

## Cognito auth: use the ID token for API calls, not the access token

AWS Cognito issues two tokens per session — the **access token** (carries
`sub` and scopes, no user identity claims) and the **ID token** (carries
`sub`, `email`, `email_verified`, name, and any custom claims). The API
verifies whichever token you send; if the backend needs the caller's email
for audit/display (e.g. `_operator` on a manual-resolution DDB marker), only
the ID token has it — sending the access token silently falls back to the
Cognito UUID and the audit trail shows meaningless GUIDs.

```ts
// WRONG — access token has no `email` claim; backend logs Cognito sub UUID
const session = await fetchAuthSession();
const token = session.tokens?.accessToken?.toString();
return { Authorization: `Bearer ${token}` };

// RIGHT — ID token carries email; backend logs "alice@example.com"
const session = await fetchAuthSession();
const token = session.tokens?.idToken?.toString();
return { Authorization: `Bearer ${token}` };
```

**Why:** hit in 1.0.0 manual-resolution flow — `_operator` on the DDB
marker was recording UUIDs like `a1b2c3d4-…` because `useAuth`'s helper
returned `session.tokens?.accessToken`. Backend `auth.verify_cognito_token`
extracts `claims.get("email")` and falls back to `sub` when absent — so the
audit trail was silently wrong. Fixed by renaming to `getIdToken()` and
routing every `Authorization` header through it.

**How to apply:** every `Authorization: Bearer` header from a Cognito
session uses the ID token. The access token is only for AWS SDK calls where
Cognito acts as an OIDC identity provider for AWS-native services — not
applicable in this codebase. If you see `session.tokens?.accessToken` in
the diff, that's the bug.

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
