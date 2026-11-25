---
area: sdk
accepted: 2026-05-27
---
# ADR #77 — TaskStatus class-level sets → generated module-level

#### Context

When ADR #72 (SSoT enums codegen, v0.79.0) consolidated 8 enum
families, the backend's hand-written `TaskStatus` class kept its
class-level sets (`TERMINAL`, `SUCCESS_STATES`, `FAILURE_STATES`,
`ACTIVE`, `WAITING_STATES`, `STOPPABLE`) intact because 8 call sites
relied on `TaskStatus.TERMINAL` etc. access patterns. The generator
emitted module-level equivalents (`TASK_TERMINAL_STATUSES` etc.) but
backend code didn't use them.

ADR #72 documented this explicitly: "Future v0.79.x will migrate the
8 sites and let the hand-written disappear." This is that release.

#### Decision

**1. Remove class-level sets** from the hand-written `TaskStatus`
class in both `_shared/constants.py` and `console_api/constants.py`.
The class keeps only the string attributes (WAITING, RUNNING, etc.).

**2. Re-export generated sets at module level** so existing import
paths still work:

```python
# At end of console_api/constants.py:
from constants_generated import (
    TASK_TERMINAL_STATUSES,
    TASK_SUCCESS_STATUSES,
    TASK_FAILURE_STATUSES,
    TASK_ACTIVE_STATUSES,
    TASK_WAITING_STATUSES,
    TASK_STOPPABLE_STATUSES,
)
```

`from constants import TASK_TERMINAL_STATUSES` now works without
touching `constants_generated` directly.

**3. Migrate 8 call sites**:

| File | Before | After |
|---|---|---|
| `evaluate_deps/index.py` | `TaskStatus.SUCCESS_STATES` | `TASK_SUCCESS_STATUSES` |
| | `TaskStatus.FAILURE_STATES` | `TASK_FAILURE_STATUSES` |
| | `TaskStatus.TERMINAL` | `TASK_TERMINAL_STATUSES` |
| `console_api/task_actions.py` | `TaskStatus.TERMINAL` (× 2) | `TASK_TERMINAL_STATUSES` |
| `console_api/routes/executions.py` | `TaskStatus.TERMINAL` | `TASK_TERMINAL_STATUSES` |
| `console_api/routes/tasks.py` | `TaskStatus.WAITING_STATES` | `TASK_WAITING_STATUSES` |

**4. Extend codegen targets to evaluate_deps.** Per ADR #72 the
generator wrote to `_shared/` + `console_api/`. evaluate_deps imports
from its synced `constants.py` (copy of `_shared/`), and that file
now does `from constants_generated import ...`. So evaluate_deps's
deploy package needs `constants_generated.py` too. Added a third
PY_TARGETS entry.

**5. `_shared/constants.py` keeps fallback constants** inside a
`try/except ImportError`. If a future Lambda copies `_shared/constants.py`
without also pulling `constants_generated.py`, it still gets working
sets (values inline-duplicated from canonical). The drift test
(`test_enum_drift.py`) catches divergence.

#### What changed about the canonical sets

The canonical `polyris.constants.TERMINAL_STATUSES` includes both
`'success'` (legacy) and `'succeeded'` (current). The
old class-level `TaskStatus.TERMINAL` in backend only had `'success'`.
After migration, `TASK_TERMINAL_STATUSES` now includes both forms.

**Behavior impact:** any code checking `status in TASK_TERMINAL_STATUSES`
will now treat `'succeeded'` as terminal where previously it might
not have. This is the correct direction — the SDK already documented
`succeeded` as an alias for `success` (migration compat), so callers
that didn't recognize it were silently buggy. Tests updated to
reflect the new set membership.

#### Tests

| Suite | Was | Now | Δ |
|---|---|---|---|
| SDK Python | 901 | 901 | 0 (2 smoke tests updated in place) |
| console_api | 348 | 348 | 0 |
| evaluate_deps | 56 | **56** | 0 (3 TestStatusCategories tests updated for canonical sets) |
| Other Lambda totals | unchanged | unchanged | 0 |
| **Python total** | **1347** | **1347** | 0 |
| vitest | 830 | 830 | 0 |
| **All gates** | green | **green** | — |

#### Two updated tests

- `tests/sdk/test_smoke.py::test_trigger_rule_sync` — checked for
  literal `'FAILURE_STATES'` string in constants.py source. Updated
  to check `'TASK_FAILURE_STATUSES'`.
- `tests/sdk/test_smoke.py::test_notify_dependents_deps_blocked` —
  same pattern, same fix.
- `evaluate_deps/test_evaluate_deps.py::TestStatusCategories` —
  3 tests updated; sets now include `'succeeded'` per canonical.

#### What this does NOT do

- **`AssetOperator`, `BackfillStatus`, etc. classes still have
  module-level sets in canonical only.** They never had class-level
  sets in backend. No migration needed.
- **Frontend already used generated.** Per ADR #72, frontend is
  fully SSoT. This release is purely backend cleanup.

#### Lesson

When ADR #72 deliberately left this migration for later, the
reasoning was "8 sites is real touch surface." Doing it 5 releases
later, the actual migration was ~15 minutes of mechanical
substitution + 3 trivial test updates. The cost was lower than
predicted because by v0.79.5 the codegen infrastructure was solid
and the call sites were all in files we'd touched recently.

Deferring mechanical cleanup is cheap when the deferral is honest —
documented in an ADR, tracked in a release plan, with a defined
trigger ("when v0.79.x lands"). Deferring it because "we don't have
time" is how technical debt accumulates without a paydown date.
