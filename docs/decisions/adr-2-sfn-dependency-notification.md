# ADR #2 — SFN-Based Dependency Notification (not polling, not EventBridge)

> **Superseded by [ADR #137](adr-137-tasks-as-rows.md) §1, §7 (1.3.0).** Dependencies are no longer resolved through `notify_dependents` and task tokens: the dispatcher decides each task row from its upstreams' rows, woken by the tasks table's stream.

**Decision:** Use Step Function helper (`notify_dependents`) + `waitForTaskToken` for dependency resolution.

**Rationale:**
- No wasted compute (polling)
- Instant notifications via SFN callback tokens
- Full visibility in Step Functions execution history (vs EventBridge "black box")
- Scales automatically

**History:** v1-v50 used EventBridge events for notifications. Replaced with SFN-based approach in v51 for better observability and debugging. Converted to Express workflow in v55.2 for cost savings (~10x cheaper, no .sync or waitForTaskToken needed). Execution history preserved via CloudWatch Logs.

**Alternative considered:** DynamoDB polling every N seconds
**Why rejected:** Expensive, slow, doesn't scale
