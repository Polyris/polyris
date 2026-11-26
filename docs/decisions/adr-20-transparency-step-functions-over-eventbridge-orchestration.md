---
area: engine
accepted: by 2026-02-24
---
# ADR #20 — Transparency: Step Functions over EventBridge for Orchestration

> **Superseded in part by [ADR #137](adr-137-tasks-as-rows.md) §1 (1.3.0).** A waiting task leaves Step Functions, and the console shows why it waits.

**Decision:** Migrate all orchestration logic from EventBridge+Lambda to Step Functions for visibility.

**History:**
- v1-v50: EventBridge events for dependency notifications + asset triggers
- v51: Replaced notify_dependents Lambda+EventBridge with SFN helper
- v55: Replaced asset_trigger Lambda+EventBridge with notify_asset_consumers SFN

**Rationale:**
- EventBridge is a "black box" — event goes in, something happens, hard to debug what/when/why
- Step Functions execution history shows every state, every input/output, every error
- When a task is stuck "waiting" — you can open SFN console and see exactly which dependency is pending
- When notification fails — you see which subscriber, which trigger_rule evaluation, which token

**Trade-off:** More state transitions = slightly higher cost (~$5/month). Worth it for debuggability in production.
