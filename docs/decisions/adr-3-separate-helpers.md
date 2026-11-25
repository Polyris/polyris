---
area: engine
accepted: by 2026-02-24
---
# ADR #3 — Separate Helpers (not monolithic wrapper)

> **Superseded by [ADR #137](adr-137-tasks-as-rows.md) §1 (1.3.0).** The helpers the wrapper was split into go with the wrapper: `run_task` runs one attempt of a task's work, and the dispatcher decides the rest.

**Decision:** Split wrapper into multiple small Step Functions.

**Rationale:**
- Easier testing
- Better error isolation
- Reusable components
- Cleaner logs

**Helpers (12):**
- registration — register task + check deps
- run_task — execute actual task (7 types)
- failure_handler — error handling + alerting
- pause_waiter — save pause token for callback
- notify_dependents — wake up waiting tasks **(EXPRESS)**
- notify_asset_consumers — cross-pipeline asset triggers **(EXPRESS)**
- restart_task — restart failed task **(EXPRESS)**
- restart_wrapper — restart wrapper execution **(EXPRESS)**
- ~~register_on_create~~ — removed, replaced by Pulumi Dynamic Provider (ADR #24)
- ~~interactive_choice_slack / pagerduty_alerter / pagerduty_resolver~~ — removed
