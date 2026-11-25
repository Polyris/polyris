---
area: engine
accepted: by 2026-02-24
---
# ADR #8 — JSONata for Step Functions (not JSONPath)

**Decision:** Use JSONata (QueryLanguage: "JSONata") throughout.

**Rationale:**
- More powerful expressions
- Cleaner syntax
- Better error handling
- Native date/time functions

**Example:**
```json
"Output": "{% $substringBefore($now(), 'T') %}"
```
