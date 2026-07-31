"""Tests for BackfillRecord value-object (v0.80.0, ADR #83).

Moved from tests/dal/test_backfills_repo.py when BackfillRecord was
extracted to the models layer.
"""

class TestBackfillRecord:
    """v0.80.0, ADR #83 — typed value-object over a Backfill DDB item.
    Centralizes status/counter semantics so consumers never read raw keys
    or re-derive done/active ad-hoc (the ADR #81/#82 bug class)."""

    def _import(self):
        from models.backfill_record import BackfillRecord
        return BackfillRecord

    def test_id_prefers_backfill_id_then_execution_name(self):
        BackfillRecord = self._import()
        assert BackfillRecord({'backfill_id': 'bf-1', 'execution_name': 'x'}).id == 'bf-1'
        assert BackfillRecord({'execution_name': 'bf-2'}).id == 'bf-2'
        assert BackfillRecord({}).id is None

    def test_counters_coerce_to_int(self):
        BackfillRecord = self._import()
        rec = BackfillRecord({
            'total_partitions': '3', 'completed_partitions': '2',
            'failed_partitions': '1', 'skipped_partitions': '4',
        })
        assert (rec.total, rec.completed, rec.failed, rec.skipped) == (3, 2, 1, 4)

    def test_missing_counters_default_zero(self):
        BackfillRecord = self._import()
        rec = BackfillRecord({})
        assert (rec.total, rec.completed, rec.failed, rec.skipped) == (0, 0, 0, 0)

    def test_is_active_and_terminal(self):
        BackfillRecord = self._import()
        assert BackfillRecord({'status': 'running'}).is_active is True
        assert BackfillRecord({'status': 'pending'}).is_active is True
        assert BackfillRecord({'status': 'completed'}).is_terminal is True
        assert BackfillRecord({'status': 'canceled'}).is_terminal is True
        assert BackfillRecord({'status': 'running'}).is_terminal is False

    def test_map_done_ignores_skipped(self):
        BackfillRecord = self._import()
        # 1 of 3 to-run still pending; a pre-flight skipped count must not
        # make it look done (the ADR #82 invariant).
        rec = BackfillRecord({
            'status': 'running', 'total_partitions': 3,
            'completed_partitions': 2, 'failed_partitions': 0,
            'skipped_partitions': 1,
        })
        assert rec.map_done is False
        assert rec.derived_status() == 'running'

    def test_derived_status_terminal_when_map_done(self):
        BackfillRecord = self._import()
        assert BackfillRecord({
            'status': 'running', 'total_partitions': 2,
            'completed_partitions': 2, 'failed_partitions': 0,
        }).derived_status() == 'completed'
        assert BackfillRecord({
            'status': 'running', 'total_partitions': 2,
            'completed_partitions': 1, 'failed_partitions': 1,
        }).derived_status() == 'partial'

    def test_derived_status_respects_terminal_raw(self):
        BackfillRecord = self._import()
        assert BackfillRecord({'status': 'canceled', 'total_partitions': 5,
                               'completed_partitions': 5}).derived_status() == 'canceled'

    def test_age_seconds(self):
        from datetime import datetime, timezone, timedelta
        BackfillRecord = self._import()
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        age = BackfillRecord({'started_at': old}).age_seconds()
        assert age is not None and age >= 290
        assert BackfillRecord({}).age_seconds() is None
        assert BackfillRecord({'started_at': 'garbage'}).age_seconds() is None
