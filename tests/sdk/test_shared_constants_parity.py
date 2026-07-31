"""Tests for _shared constants parity checker (v0.80.0, ADR #83)."""
from polyris.codegen.check_shared_constants import (
    check_shared_constants,
    _class_values,
    _load_shared_module,
)
from polyris import constants as canon


class TestSharedConstantsParity:
    def test_committed_shared_in_parity(self):
        """The shipped _shared/constants.py must not define any status value
        absent from canonical polyris/constants.py."""
        assert check_shared_constants() == 0

    def test_shared_taskstatus_subset_of_canonical(self):
        sh = _load_shared_module()
        canon_vals = {m.value for m in canon.TaskStatus}
        assert _class_values(sh.TaskStatus) <= canon_vals

    def test_dead_members_removed(self):
        """DEFAULT / EARLY_TRIGGER were dead and removed in v0.80.0."""
        sh = _load_shared_module()
        assert not hasattr(sh.TriggerRule, 'DEFAULT')
        assert not hasattr(sh.TriggerRule, 'EARLY_TRIGGER')
