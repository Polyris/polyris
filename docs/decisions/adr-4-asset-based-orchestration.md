---
area: engine
accepted: by 2026-02-24
---
# ADR #4 — Asset-Based Orchestration (not ExternalTaskSensor)

**Decision:** Implement asset triggers.

**Rationale:**
- Decoupled pipelines
- No hardcoded DAG references
- Natural data lineage
- Better than sensors (no polling)
