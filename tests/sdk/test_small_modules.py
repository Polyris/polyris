"""Small-module coverage — partition guards, cadence, status, resolver, context.

Closes the defensive ``ValueError`` guards in ``partitions``, the unknown-unit
return in ``granularity.infer_cron_cadence``, every branch of
``constants.normalize_execution_status``, the ``ARNResolver`` load-failure path,
and ``DAG.get_current_context`` (CLAUDE.md #13).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polyris import partitions as P
from polyris.granularity import infer_cron_cadence
from polyris.constants import (
    normalize_execution_status,
    EXECUTION_STATUS_CANONICAL,
    _EXECUTION_STATUS_UPPERCASE_MAP,
)
from polyris.resolver import ARNResolver
from polyris.dag import DAG

DT = datetime(2025, 1, 1, tzinfo=timezone.utc)


class TestPartitionGuards:
    def test_parse_unknown_granularity(self):
        with pytest.raises(ValueError):
            P._parse_to_datetime("2025-01-01", "bogus")

    def test_floor_unknown_granularity(self):
        with pytest.raises(ValueError):
            P._floor_to_bucket(DT, "bogus")

    def test_advance_unknown_granularity(self):
        with pytest.raises(ValueError):
            P._advance(DT, "bogus")

    def test_format_unknown_granularity(self):
        with pytest.raises(ValueError):
            P._format_key(DT, "bogus")

    def test_covering_invalid_target_granularity(self):
        with pytest.raises(ValueError):
            P.partitions_covering("2025-01-01", "bogus", "daily")


class TestGranularityInference:
    def test_rate_with_unknown_unit_returns_none(self):
        assert infer_cron_cadence("rate(5 weeks)") is None


class TestNormalizeExecutionStatus:
    def test_none_returns_none(self):
        assert normalize_execution_status(None) is None

    def test_canonical_passes_through(self):
        canon = next(iter(EXECUTION_STATUS_CANONICAL))
        assert normalize_execution_status(canon) == canon

    def test_uppercase_is_mapped(self):
        key, val = next(iter(_EXECUTION_STATUS_UPPERCASE_MAP.items()))
        assert normalize_execution_status(key) == val

    def test_unknown_warns_then_passes_through(self):
        captured = {}

        def log_warn(_msg, **kw):
            captured.update(kw)

        assert normalize_execution_status("zzz_unknown", log_warn=log_warn) == "zzz_unknown"
        assert captured.get("status") == "zzz_unknown"


class TestArnResolverLoadFailure:
    def test_malformed_tasks_json_is_warned(self, tmp_path, capsys):
        (tmp_path / "tasks.json").write_text("{ this is not valid json ")
        ARNResolver(pipeline_path=tmp_path / "dag.py")  # must not raise
        assert "Could not load tasks.json" in capsys.readouterr().out


class TestDagCurrentContext:
    def test_get_current_context_outside_is_none(self):
        assert DAG.get_current_context() is None


class TestLabelReverseShift:
    def test_step_rshift_label_sets_upstream(self):
        # Step.__rshift__ returns NotImplemented for a Label, so Python falls
        # back to Label.__rrshift__, which records the upstream and returns self.
        from polyris.helpers import Label
        from polyris.steps import Wait

        result = Wait(seconds=5) >> Label("edge")
        assert isinstance(result, Label)
        assert result.label == "edge"
        assert result._upstream is not None
