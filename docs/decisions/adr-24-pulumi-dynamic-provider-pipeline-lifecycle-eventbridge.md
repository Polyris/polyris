# ADR #24 — Pulumi Dynamic Provider for Pipeline Lifecycle (not EventBridge)

**Superseded — the Pulumi Dynamic Provider was replaced by `polyris-deploy` (SAM/CFN + direct DynamoDB `UpdateItem`) in the current release. See [REGISTRATION.md](../features/REGISTRATION.md) for the current mechanism.**

**Decision (historical):** Use Pulumi Dynamic Provider (`PipelineRegistration`) for pipeline registration/deregistration instead of EventBridge + CloudTrail auto-registration.

**Problem:** EventBridge auto-registration had multiple failure modes:
- CloudTrail event propagation: 1-15 minute delay
- Fresh environments: no CloudTrail trail configured → registration never fires
- Asset-triggered pipelines: chicken-egg deadlock (needs subscriptions to trigger, needs execution to register)
- No cleanup on destroy: zombie pipelines in UI sidebar, orphaned asset subscriptions

**Dynamic Provider lifecycle:**
- `create`: `StartExecution(register_only=true)` — 2-3 second latency
- `diff`: compares `dag_hash` — skips if unchanged
- `update`: re-registers with new DAG
- `delete`: direct DDB cleanup (`pipeline_registry` + `asset_subscriptions`)

**Why not `apply()` callback:** `apply()` fires on every `polyris-deploy` (even no-change), has no error tracking, can't distinguish create from update, and doesn't work for destroy. Dynamic Provider gives Pulumi state tracking, retry on failure, and separate handlers for each lifecycle event.

**Migration:** EventBridge auto-registration (`auto_registration.tf`, ~300 lines + Lambda) removed in v69.1 after Dynamic Provider was verified in production.
