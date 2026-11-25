# ADR #13 — Decorator Parameter Duplication (for IDE autocomplete)

**Decision:** Duplicate common parameters (`retries`, `retry_delay`, `trigger_rule`, `wait_before`, `skip_on_backfill`) across all 7 task decorator methods.

**Rationale:**
- Python lacks good mechanism for "signature inheritance" between methods
- Alternatives (TypedDict, `**kwargs`, ParamSpec) all break IDE hints
- Established orchestrators use same pattern
- ~10 params × 7 decorators = acceptable duplication for perfect IDE experience

**Alternative considered:** Base class with shared params
**Why rejected:** No IDE autocomplete for inherited method signatures
