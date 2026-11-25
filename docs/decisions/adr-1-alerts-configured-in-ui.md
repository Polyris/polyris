---
area: console-api
accepted: by 2026-02-24
---
# ADR #1 — Alerts Configured in the UI, Not the DSL

**Decision:** Failure-alert delivery is configured per pipeline
in the Settings → Alerts UI and stored in the pipeline registry (`alert_config`),
not declared in the DAG. The old `alerts=` DAG parameter has been **removed**.

**Rationale:**
- Secrets (webhook URLs, routing keys) must not live in pipeline source —
  they belong in SSM, written via the UI, never committed
- Alerting is an operational concern that changes without a code redeploy
- A single source of truth (the registry) that the notify Lambda reads at 3am,
  with no UI involvement at failure time

**Implementation:**
The DSL no longer defines an `alerts=` parameter; passing it raises a `TypeError`.
Non-secret config (channel, mentions, severity,
enabled channels) lives in `alert_config`; secrets go to SSM with only the
parameter name kept in the config. See `docs/features/notifications.md`.
