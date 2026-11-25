---
area: ui
accepted: by 2026-02-24
---
# ADR #6 — Polling for UI Updates (simple and reliable)

**Decision:** Use polling (3s active / 30s idle) for UI updates.

**Rationale:**
- Simple implementation
- No WebSocket infrastructure needed
- Works through all proxies/firewalls
- Sufficient for dashboard use case

**Implementation:**
- Active polling (3s) when tasks are running
- Idle polling (30s) when no active tasks
- Auto-detect based on task statuses
