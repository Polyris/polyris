"""The readiness decision table (ADR #130 §3)."""

from datetime import date, datetime, timezone

import pytest

from polyris.coordination import decide as d
from polyris.coordination.asset_conditions import AssetResult, Evaluation, Fact


def task(status="waiting", *, pause_token=False, rule="all_success", deps=(), assets=False):
    return d.TaskView(status, pause_token, rule, tuple(deps), assets)


READY_ASSETS = Evaluation(True, (AssetResult("ns/a", "latest", True, "ok"),))
MISSING_ASSETS = Evaluation(False, (AssetResult("ns/a", "latest", False, "no_fact"),))


class TestNoop:
    def test_missing_row(self):
        assert d.decide(None).verdict == d.NOOP

    @pytest.mark.parametrize("status", ["deps_ready", "waiting_delay", "waiting_decision", "running", "success", "stopped"])
    def test_rows_owned_by_someone_else(self, status):
        assert d.decide(task(status)).verdict == d.NOOP

    def test_rows_held_by_pause_waiter(self):
        assert d.decide(task("waiting_paused", pause_token=True)).verdict == d.NOOP


class TestDependencies:
    def test_no_conditions_at_all_is_ready(self):
        assert d.decide(task()).verdict == d.READY

    def test_waiting_for_a_dependency(self):
        decision = d.decide(task(deps=["up"]), dep_statuses=["waiting"])
        assert decision.verdict == d.WAIT
        assert decision.deps.counts.pending == 1

    @pytest.mark.parametrize("statuses,verdict", [(["failed"], d.UPSTREAM_FAILED), (["skipped"], d.READY)])
    def test_terminal_dependencies(self, statuses, verdict):
        assert d.decide(task(deps=["up"]), dep_statuses=statuses).verdict == verdict

    def test_blocked_dependencies_ignore_assets(self):
        decision = d.decide(task(rule="all_skipped", deps=["up"], assets=True), dep_statuses=["success"], assets=MISSING_ASSETS)
        assert decision.verdict == d.SKIP

    def test_status_count_must_match(self):
        with pytest.raises(ValueError):
            d.decide(task(deps=["a", "b"]), dep_statuses=["success"])


class TestAssets:
    def test_assets_ready(self):
        assert d.decide(task(assets=True), assets=READY_ASSETS).verdict == d.READY

    def test_assets_missing(self):
        decision = d.decide(task(assets=True), assets=MISSING_ASSETS)
        assert decision.verdict == d.WAIT
        assert decision.reasons == ("ns/a: no_fact",)

    def test_satisfied_or_group_with_an_unready_member_is_ready(self):
        evaluation = Evaluation(True, (
            AssetResult("ns/a", "latest", False, "no_fact"),
            AssetResult("ns/b", "latest", True, "ok"),
        ))
        assert d.decide(task(assets=True), assets=evaluation).verdict == d.READY

    def test_assets_may_be_skipped_while_dependencies_wait(self):
        decision = d.decide(task(deps=["up"], assets=True), dep_statuses=["waiting"])
        assert decision.verdict == d.WAIT
        assert decision.assets is None

    def test_both_reasons_when_both_wait(self):
        decision = d.decide(task(deps=["up"], assets=True), dep_statuses=["waiting"], assets=MISSING_ASSETS)
        assert decision.verdict == d.WAIT
        assert len(decision.reasons) == 2

    def test_unevaluated_assets_are_a_caller_bug(self):
        with pytest.raises(ValueError):
            d.decide(task(assets=True))


class TestPause:
    def test_ready_but_paused(self):
        assert d.decide(task(), paused=True).verdict == d.PAUSED

    def test_waiting_paused_row_wakes_when_unpaused(self):
        assert d.decide(task("waiting_paused")).verdict == d.READY


def test_every_verdict_is_listed():
    assert {d.NOOP, d.SKIP, d.UPSTREAM_FAILED, d.WAIT, d.PAUSED, d.READY} == d.VERDICTS


def test_fact_is_a_value_object():
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert Fact("a", date(2026, 1, 1), when, "daily") == Fact("a", date(2026, 1, 1), when, "daily")
