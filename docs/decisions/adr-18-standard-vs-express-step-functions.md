# ADR #18 — Standard vs Express Step Functions

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §1 (1.3.0).** The Express helpers and the wrapper go; `run_task`, `bulk_backfill` and the test state machines stay Standard.

**Decision:** Use Standard for long-running/debuggable workflows, Express only for fire-and-forget helpers.

| Express (cheap, no history) | Standard (visible, debuggable) |
|----|---|
| notify_dependents | dependency_wrapper (hours, waitForTaskToken) |
| notify_asset_consumers | run_task (hours, .sync callbacks) |
| | registration (waitForTaskToken) |
| | failure_handler (invokes other SFNs) |

**Rationale:**
- Express: ~100x cheaper ($0.000001 vs $0.000025 per transition), but no execution history in console
- Standard: execution history visible in AWS console — critical for debugging stuck tasks
- notify_dependents is fire-and-forget (evaluate deps, send tokens) — Express OK
- run_task can run for hours (ECS/Glue jobs) — must be Standard
- Registration uses waitForTaskToken — Express doesn't support it

**Cost impact:** Express helpers save ~$5-10/month at 60K tasks. Small but adds up at scale.
