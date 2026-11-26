---
area: engine
accepted: by 2026-02-24
---
# ADR #15 — Upstream Read-Time Truncation (25KB per dependency)

**Decision:** Truncate each dependency output to 25KB when reading for downstream, not when writing.

**Rationale:**
- Step Functions has 256KB payload limit between states
- 10 deps × 25KB = 250KB = safe under limit
- Full output (200KB) preserved in DynamoDB — only in-flight payload trimmed
- Truncated output includes `_warning` field explaining what happened and how to fix
- Write-time truncation would lose data permanently; read-time preserves it
