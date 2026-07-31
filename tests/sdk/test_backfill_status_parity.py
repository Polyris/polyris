"""Backfill status parity tests (v0.80.0, ADR #83).

Locks the canonical terminal-status rule and verifies the SFN Finalize
JSONata stays in parity with it.
"""
import json


from polyris.backfill_status import finalize_status, all_map_done
from polyris.constants import BackfillStatus, BACKFILL_TERMINAL_STATUSES
from polyris.codegen.check_backfill_status_parity import (
    check_parity,
    extract_finalize_jsonata,
    TEMPLATE,
)


class TestCanonicalRule:
    def test_no_failures_is_completed(self):
        assert finalize_status(5, 0) == BackfillStatus.COMPLETED

    def test_all_failures_is_failed(self):
        assert finalize_status(0, 3) == BackfillStatus.FAILED

    def test_mixed_is_partial(self):
        assert finalize_status(2, 2) == BackfillStatus.PARTIAL

    def test_canceled_wins_regardless(self):
        for c, f in [(0, 0), (5, 0), (0, 3), (2, 2)]:
            assert finalize_status(c, f, canceled=True) == BackfillStatus.CANCELED

    def test_terminal_status_is_always_canonical(self):
        # Every (completed, failed) combo maps to exactly one terminal value.
        for c in range(4):
            for f in range(4):
                assert finalize_status(c, f) in BACKFILL_TERMINAL_STATUSES


class TestMapDone:
    def test_excludes_skipped_from_done_check(self):
        # total is the to-RUN count; only completed+failed count toward done.
        assert all_map_done(total=3, completed=2, failed=0) is False  # 1 left
        assert all_map_done(total=3, completed=2, failed=1) is True
        assert all_map_done(total=3, completed=3, failed=0) is True

    def test_zero_total_is_not_done(self):
        assert all_map_done(total=0, completed=0, failed=0) is False


class TestSfnParity:
    def test_committed_template_in_parity(self):
        """The shipped bulk_backfill SFN Finalize must match the canonical
        rule. If this fails, the SFN and Python derivation have drifted."""
        assert check_parity() == 0

    def test_finalize_does_not_reference_skipped(self):
        """ADR #82 guard: the Finalize aggregate must not use 'skipped'."""
        expr = extract_finalize_jsonata()
        assert 'skipped' not in expr

    def test_drift_detected_when_jsonata_mutated(self, tmp_path, monkeypatch):
        """A Finalize formula that drops the canceled branch must be caught."""
        original = json.loads(TEMPLATE.read_text())
        # Corrupt the Finalize Output: remove the canceled branch.
        bad_output = original['States']['Finalize']['Output'].replace(
            "$currentStatus = 'canceled' ? 'canceled' : ", ""
        )
        original['States']['Finalize']['Output'] = bad_output
        bad_file = tmp_path / "sfn.tpl.json"
        bad_file.write_text(json.dumps(original))
        monkeypatch.setattr(
            'polyris.codegen.check_backfill_status_parity.TEMPLATE', bad_file,
        )
        assert check_parity() == 1

    def test_drift_detected_when_skipped_reintroduced(self, tmp_path, monkeypatch):
        """If a future edit puts 'skipped' back into the aggregate, fail."""
        original = json.loads(TEMPLATE.read_text())
        out = original['States']['Finalize']['Output']
        # Inject a skipped reference into the aggregate.
        out = out.replace('$failed := ', '$skipped := 0; $failed := ')
        original['States']['Finalize']['Output'] = out
        bad_file = tmp_path / "sfn.tpl.json"
        bad_file.write_text(json.dumps(original))
        monkeypatch.setattr(
            'polyris.codegen.check_backfill_status_parity.TEMPLATE', bad_file,
        )
        assert check_parity() == 1
