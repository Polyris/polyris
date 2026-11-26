---
area: ui
accepted: by 2026-02-24
---
# ADR #7 — Code Splitting for UI Bundle

**Decision:** Split vendor chunks (React, ReactFlow).

**Rationale:**
- Vendor chunks cached separately
- Faster subsequent loads
- Main bundle < 150KB

**Chunks:**
- index.js (~125KB) - app code
- vendor-react.js (~141KB) - React core
- vendor-flow.js (~238KB) - ReactFlow + dagre
