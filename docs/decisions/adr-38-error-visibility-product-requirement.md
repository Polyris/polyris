---
area: engine
accepted: by 2026-05-02
---
# ADR #38 — Error Visibility — Product Requirement

**Context:** During development we found that errors in various layers of the system were silently lost.
Classic example: `query_subscriptions` returned `{'subscribers': []}` instead of an error when
it lacked permissions on a table — downstream tasks hung with no signal in UI or CloudWatch.

**Requirement:** Every error in the system must be visible to the operator. Visibility levels:

| Level | Visible to | When to use |
|-------|-----------|-------------|
| **UI (Notifications bell)** | All users | Task failures, infrastructure errors that block a pipeline |
| **CloudWatch Logs** | Operator/DevOps | All other errors, debug details |

**Lambda rules:**
1. Never swallow `AccessDeniedException` — always `raise`
2. Every `except Exception` block must log `error=str(e)` with context
3. If an error blocks downstream tasks — write a `_notify_warn_` record to `pipeline-tokens`
   so the Notifications bell automatically surfaces it in the UI

**SFN template rules:**
1. `Catch` blocks must preserve `$states.errorOutput` in Output — do not lose context
2. Errors in Express SFNs must either propagate via `Catch` → `failure_handler`, or be logged in CloudWatch (ALL level)

**`_notify_warn_` pattern implementation:** superseded by ADR #130 §9. A warning row is built
with `records.warning_item(kind=…)` and named `_notify_warn_{kind}_{execution_name}`; it carries
no `date`, and the bell reads it from `attention-index` by write time. The date-keyed row this
ADR first described reached the bell only while its date was in the bell's window.

**Important:** Records with `execution_name.startswith('_')` are internal/special.
All loops iterating `pipeline-tokens` items MUST filter such records:
```python
for item in items:
    if item.get('execution_name', '').startswith('_'):
        continue
```
This pattern is already applied throughout `console_api/routes/`.

**Special prefixes in `pipeline-tokens`:**
- `_pause_{pipeline_execution}` — pause state record
- `_notify_warn_{kind}_{execution_name}` — infrastructure warning visible in UI (ADR #130 §9)
