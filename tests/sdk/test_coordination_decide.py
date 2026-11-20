"""The readiness decision of a waiting row, which a pass of the dispatcher makes (ADR #130 §3, ADR #137 §1)."""

from datetime import date, datetime, timezone

import pytest

from polyris.coordination import decide as d
from polyris.coordination.asset_conditions import AssetResult, Evaluation, Fact


def task(status="waiting", *, rule="all_success", deps=(), assets=False):
    return d.TaskView(status, rule, tuple(deps), assets)


READY_ASSETS = Evaluation(True, (AssetResult("ns/a", "latest", True, "ok"),))
MISSING_ASSETS = Evaluation(False, (AssetResult("ns/a", "latest", False, "no_fact"),))


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
    def test_waiting_paused_row_wakes_when_unpaused(self):
        assert d.decide(task("waiting_paused")).verdict == d.READY


def test_every_verdict_is_listed():
    assert {d.SKIP, d.UPSTREAM_FAILED, d.WAIT, d.READY, d.FAIL} == d.VERDICTS


@pytest.mark.parametrize("dependencies", [(), ("extract",)])
def test_an_unknown_rule_fails_its_task_a_root_task_included(dependencies):
    """ADR #138 §2: not evaluated as all_success, whatever the upstreams' statuses."""
    view = d.TaskView(status="waiting", trigger_rule="always",
                      dependencies=dependencies, has_asset_conditions=True)
    decision = d.decide(view, dep_statuses=["success"] * len(dependencies))
    assert decision.verdict == d.FAIL and "remove the dependency" in decision.reasons[0]


def test_a_dependencys_skip_origin_reaches_its_rule():
    view = d.TaskView(status="waiting", trigger_rule="all_success",
                      dependencies=("extract",), has_asset_conditions=False)
    assert d.decide(view, dep_statuses=["skipped"], skip_origins=["rule"]).verdict == d.SKIP
    assert d.decide(view, dep_statuses=["skipped"], skip_origins=["manual"]).verdict == d.READY


def test_fact_is_a_value_object():
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert Fact("a", date(2026, 1, 1), when, "daily") == Fact("a", date(2026, 1, 1), when, "daily")


def test_a_wait_records_reasons_with_no_time_in_them():
    """ADR #137 §1: a pass over a run where nothing changed writes nothing, so the reasons hold no
    time or count that changes while the task still waits: the same wait gives the same reasons."""
    stale_then = Evaluation(False, (AssetResult("ns/a", "within", False, "stale",
                                                Fact("ns/a", date(2026, 11, 17), datetime(2026, 11, 17, 3, tzinfo=timezone.utc),
                                                     "daily")),))
    stale_now = Evaluation(False, (AssetResult("ns/a", "within", False, "stale",
                                               Fact("ns/a", date(2026, 11, 17), datetime(2026, 11, 17, 9, tzinfo=timezone.utc),
                                                    "daily")),))
    waiting_on_two = d.decide(task(deps=("x", "y"), assets=True), dep_statuses=["running", "waiting"])
    waiting_on_one = d.decide(task(deps=("x", "y"), assets=True), dep_statuses=["success", "running"])
    assert waiting_on_two.reasons == waiting_on_one.reasons == ("waiting for its upstreams",)
    assert d.decide(task(assets=True), assets=stale_then).reasons == d.decide(task(assets=True), assets=stale_now).reasons
