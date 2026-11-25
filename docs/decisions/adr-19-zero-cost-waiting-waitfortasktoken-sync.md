# ADR #19 — Zero-Cost Waiting (waitForTaskToken + .sync)

> **Amended by [ADR #137](adr-137-tasks-as-rows.md) §1 (1.3.0).** A waiting task costs nothing because it is a row, not because it waits on a token.

**Decision:** Use Step Functions callback patterns for all waiting instead of polling.

**Rationale:**
- `waitForTaskToken`: wrapper waits for dependency resolution — $0 while waiting (no transitions)
- `.sync`: run_task waits for ECS/Glue/Lambda completion — $0 while waiting
- A task can wait for hours or days — cost is always 0 (Standard SFN charges per transition, not per second)

**Comparison at 2000 tasks/day, 1.5hr average:**
| Pattern | Monthly cost for waiting |
|---|---:|
| polyris (waitForTaskToken) | **$0** |
| Managed orchestrators (MWAA) | ~$4,950 |
| Prefect (polling agent) | ~$53 |
| Dagster (polling daemon) | ~$35 |

This is polyris's key architectural advantage — the longer tasks run, the more money is saved versus polling-based orchestrators.
