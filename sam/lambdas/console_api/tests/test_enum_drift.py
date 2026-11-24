"""
Drift guard for backend constants (v0.79.0, ADR #72).

The hand-written enum classes in console_api/constants.py (TaskStatus,
TriggerRule, BackfillStatus, etc.) MUST stay in sync with the canonical
source polyris/constants.py. The generator (polyris.codegen.sync_enums)
produces constants_generated.py from canonical; this test asserts the
hand-written ones agree.

Why both exist for v0.79.0:
- Hand-written has class-level sets (TaskStatus.TERMINAL) used at 8 sites
- Generated has module-level sets (TASK_TERMINAL_STATUSES) for SDK style
- Migration to fully-generated backend is incremental; this test catches
  drift in the meantime.
"""
import sys
import os

# Make the SDK constants importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..', '..'))

import constants as backend  # console_api/constants.py
from polyris import constants as canonical


class TestEnumDrift:
    def test_task_status_values_match_canonical(self):
        backend_values = {
            getattr(backend.TaskStatus, n)
            for n in vars(backend.TaskStatus)
            if not n.startswith('_') and isinstance(
                getattr(backend.TaskStatus, n), str
            )
        }
        canonical_values = {m.value for m in canonical.TaskStatus}
        # Backend MAY have a subset of canonical (legacy: missing SUCCEEDED).
        # But every backend value MUST exist in canonical.
        missing = backend_values - canonical_values
        assert not missing, (
            f"Backend TaskStatus has values not in canonical: {missing}. "
            f"Add them to polyris/constants.py or remove from backend."
        )

    def test_settled_statuses_is_terminal_plus_stopped(self):
        # SETTLED = terminal, or deliberately stopped (restartable). Single source of
        # truth (ADR #112 audit) — used for task-feed skipping and abort reconciliation
        # instead of a re-listed literal.
        assert backend.TASK_SETTLED_STATUSES == canonical.SETTLED_STATUSES
        assert backend.TASK_SETTLED_STATUSES == backend.TASK_TERMINAL_STATUSES | {'stopped'}
        # 'stopped' is the only non-terminal member.
        assert backend.TASK_SETTLED_STATUSES - backend.TASK_TERMINAL_STATUSES == {'stopped'}

    def test_trigger_rule_values_match_canonical(self):
        backend_values = {
            getattr(backend.TriggerRule, n)
            for n in vars(backend.TriggerRule)
            if not n.startswith('_') and isinstance(
                getattr(backend.TriggerRule, n), str
            )
        }
        canonical_values = {
            getattr(canonical.TriggerRule, n)
            for n in vars(canonical.TriggerRule)
            if not n.startswith('_')
        }
        assert backend_values == canonical_values, (
            f"TriggerRule drift: backend={backend_values}, "
            f"canonical={canonical_values}"
        )

    def test_backfill_status_values_match_canonical(self):
        backend_values = {
            getattr(backend.BackfillStatus, n)
            for n in vars(backend.BackfillStatus)
            if not n.startswith('_') and isinstance(
                getattr(backend.BackfillStatus, n), str
            )
        }
        canonical_values = {
            canonical.BackfillStatus.PENDING,
            canonical.BackfillStatus.RUNNING,
            canonical.BackfillStatus.COMPLETED,
            canonical.BackfillStatus.FAILED,
            canonical.BackfillStatus.PARTIAL,
            canonical.BackfillStatus.CANCELED,
        }
        assert backend_values == canonical_values, (
            f"BackfillStatus drift: backend={backend_values}, "
            f"canonical={canonical_values}"
        )

    def test_generated_in_sync_with_canonical(self):
        # The generator must produce identical bytes on re-run; CI also
        # checks this via `make check-generate-enums`, but unit test
        # gives early feedback.
        from polyris.codegen.sync_enums import check_all
        changes = check_all()
        drifted = [str(p) for p, changed in changes.items() if changed]
        assert not drifted, (
            f"Generated enum files out of sync with polyris/constants.py: "
            f"{drifted}. Run: python -m polyris.codegen.sync_enums"
        )
