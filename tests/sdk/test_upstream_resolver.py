"""Tests for polyris.upstream_resolver and partitions.partitions_covering.

Covers the cross-pipeline tiered resolver (ADR #88) on the intersecting-
window mapping (ADR #87), including the production aspects beyond the
Phase 0.5 spike: collected warnings, dag_hash recording (ADR #89 R5), and
the reserved-but-warned window-offset surface (ADR #89 R2).

Pure stdlib, no AWS — fast.
"""
from __future__ import annotations

import pytest

from polyris.partitions import partitions_covering
from polyris.upstream_resolver import (
    AssetGraph,
    AssetNode,
    CycleError,
    ResolvedPlan,
    resolve_plan,
)


# ===========================================================================
# partitions_covering — intersecting-window mapping (ADR #87)
# ===========================================================================

class TestPartitionsCovering:
    def test_same_granularity_is_1to1(self):
        assert partitions_covering("2026-05-20", "daily", "daily") == ["2026-05-20"]

    def test_daily_target_covers_24_hourly(self):
        keys = partitions_covering("2026-05-20", "daily", "hourly")
        assert keys[0] == "2026-05-20T00"
        assert keys[-1] == "2026-05-20T23"
        assert len(keys) == 24

    def test_weekly_covers_7_daily(self):
        keys = partitions_covering("2026-W21", "weekly", "daily")
        assert keys == [
            "2026-05-18", "2026-05-19", "2026-05-20", "2026-05-21",
            "2026-05-22", "2026-05-23", "2026-05-24",
        ]

    def test_monthly_covers_daily_non_leap(self):
        keys = partitions_covering("2026-02", "monthly", "daily")
        assert keys[0] == "2026-02-01"
        assert keys[-1] == "2026-02-28"
        assert len(keys) == 28

    def test_monthly_covers_daily_leap(self):
        keys = partitions_covering("2024-02", "monthly", "daily")
        assert keys[-1] == "2024-02-29"
        assert len(keys) == 29

    def test_finer_target_maps_to_containing_bucket(self):
        assert partitions_covering("2026-05-20T14", "hourly", "daily") == ["2026-05-20"]

    def test_invalid_granularity_raises(self):
        with pytest.raises(ValueError):
            partitions_covering("2026-05-20", "daily", "yearly")


# ===========================================================================
# Graph fixtures
# ===========================================================================

def _linear_graph():
    """raw -> staged -> catalog, each in its own pipeline, all daily."""
    g = AssetGraph()
    for a, p, h in [("raw", "p_raw", "h_raw"), ("staged", "p_stage", "h_stage"),
                    ("catalog", "p_cat", "h_cat")]:
        g.add_node(AssetNode(a, p, "daily", dag_hash=h))
    g.add_edge("catalog", "staged")
    g.add_edge("staged", "raw")
    return g


def _diamond_graph():
    g = AssetGraph()
    for a in ["base", "left", "right", "top"]:
        g.add_node(AssetNode(a, f"p_{a}", "daily"))
    g.add_edge("top", "left")
    g.add_edge("top", "right")
    g.add_edge("left", "base")
    g.add_edge("right", "base")
    return g


def NONE_EXIST(a, p, k): return False
def ALL_EXIST(a, p, k): return True


def _order(plan: ResolvedPlan):
    return {i.asset: t for t, tier in enumerate(plan.tiers) for i in tier}


# ===========================================================================
# Tier ordering
# ===========================================================================

class TestTierOrdering:
    def test_linear_chain_upstream_first(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(), NONE_EXIST)
        o = _order(plan)
        assert o["raw"] < o["staged"] < o["catalog"]
        assert o["catalog"] == len(plan.tiers) - 1

    def test_target_in_last_tier(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(), NONE_EXIST)
        last_tier = plan.tiers[-1]
        assert [i.asset for i in last_tier] == ["catalog"]


# ===========================================================================
# Reuse / build modes
# ===========================================================================

class TestModes:
    def test_target_always_built(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(),
                            ALL_EXIST, mode="smart")
        target = [i for i in plan.all_items if i.asset == "catalog"][0]
        assert target.reused is False

    def test_smart_reuses_existing_upstream(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(),
                            ALL_EXIST, mode="smart")
        ups = [i for i in plan.all_items if i.asset in ("raw", "staged")]
        assert all(i.reused for i in ups)

    def test_smart_builds_missing_upstream(self):
        def exists(a, p, k): return a == "raw"
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(),
                            exists, mode="smart")
        by = {i.asset: i for i in plan.all_items}
        assert by["raw"].reused is True
        assert by["staged"].reused is False

    def test_force_rebuilds_all_upstream(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(),
                            ALL_EXIST, mode="force")
        ups = [i for i in plan.all_items if i.asset in ("raw", "staged")]
        assert all(i.reused is False for i in ups)

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            resolve_plan("catalog", ["2026-05-20"], _linear_graph(),
                        NONE_EXIST, mode="bogus")


# ===========================================================================
# Diamonds + cycles (the subtle tier-correctness cases)
# ===========================================================================

class TestDiamondAndCycle:
    def test_diamond_dedups_shared_upstream(self):
        plan = resolve_plan("top", ["2026-05-20"], _diamond_graph(), NONE_EXIST)
        base = [i for i in plan.all_items if i.asset == "base"]
        assert len(base) == 1

    def test_diamond_base_earliest(self):
        plan = resolve_plan("top", ["2026-05-20"], _diamond_graph(), NONE_EXIST)
        o = _order(plan)
        assert o["base"] < o["left"] and o["base"] < o["right"]
        assert o["left"] < o["top"] and o["right"] < o["top"]

    def test_asymmetric_diamond_deeper_path_wins(self):
        # top -> mid -> base AND top -> base : base must be earliest tier
        g = AssetGraph()
        for a in ["base", "mid", "top"]:
            g.add_node(AssetNode(a, f"p_{a}", "daily"))
        g.add_edge("top", "mid")
        g.add_edge("top", "base")
        g.add_edge("mid", "base")
        plan = resolve_plan("top", ["2026-05-20"], g, NONE_EXIST)
        base = [i for i in plan.all_items if i.asset == "base"]
        assert len(base) == 1
        o = _order(plan)
        assert o["base"] == 0
        assert o["base"] < o["mid"] < o["top"]

    def test_cycle_raises(self):
        g = AssetGraph()
        for a in ["a", "b"]:
            g.add_node(AssetNode(a, f"p_{a}", "daily"))
        g.add_edge("a", "b")
        g.add_edge("b", "a")
        with pytest.raises(CycleError):
            resolve_plan("a", ["2026-05-20"], g, NONE_EXIST)


# ===========================================================================
# Cross-granularity within a full plan
# ===========================================================================

class TestCrossGranularity:
    def test_daily_target_hourly_upstream(self):
        g = AssetGraph()
        g.add_node(AssetNode("events", "p_events", "hourly"))
        g.add_node(AssetNode("catalog", "p_cat", "daily"))
        g.add_edge("catalog", "events")
        plan = resolve_plan("catalog", ["2026-05-20"], g, NONE_EXIST)
        events = [i for i in plan.all_items if i.asset == "events"]
        assert len(events) == 24
        o = _order(plan)
        assert o["events"] < o["catalog"]


# ===========================================================================
# Multi-partition target
# ===========================================================================

class TestMultiPartition:
    def test_each_target_partition_expands(self):
        plan = resolve_plan("catalog", ["2026-05-19", "2026-05-20"],
                            _linear_graph(), NONE_EXIST)
        cats = {i.partition for i in plan.all_items if i.asset == "catalog"}
        raws = {i.partition for i in plan.all_items if i.asset == "raw"}
        assert cats == {"2026-05-19", "2026-05-20"}
        assert raws == {"2026-05-19", "2026-05-20"}


# ===========================================================================
# Production aspects beyond the spike: dag_hash (R5), offset warn (R2)
# ===========================================================================

class TestDagHashRecording:
    def test_plan_items_carry_dag_hash(self):
        plan = resolve_plan("catalog", ["2026-05-20"], _linear_graph(), NONE_EXIST)
        by = {i.asset: i for i in plan.all_items}
        assert by["raw"].dag_hash == "h_raw"
        assert by["catalog"].dag_hash == "h_cat"


class TestWindowOffsetReserved:
    def test_offset_edge_warns_and_resolves_1to1(self):
        g = AssetGraph()
        g.add_node(AssetNode("base", "p_base", "daily"))
        g.add_node(AssetNode("roll", "p_roll", "daily"))
        # declare a 7-day window offset — Phase 1 must NOT honor it yet
        g.add_edge("roll", "base", offset=(-6, 0))
        plan = resolve_plan("roll", ["2026-05-20"], g, NONE_EXIST)
        base = [i for i in plan.all_items if i.asset == "base"]
        assert len(base) == 1  # resolved 1↔1, not 7
        assert base[0].partition == "2026-05-20"
        assert any("not yet honored" in w for w in plan.warnings)

    def test_offset_warning_deduped(self):
        g = AssetGraph()
        g.add_node(AssetNode("base", "p_base", "daily"))
        g.add_node(AssetNode("roll", "p_roll", "daily"))
        g.add_edge("roll", "base", offset=(-6, 0))
        # multiple target partitions would fire the same warning repeatedly
        plan = resolve_plan("roll", ["2026-05-19", "2026-05-20", "2026-05-21"],
                            g, NONE_EXIST)
        offset_warnings = [w for w in plan.warnings if "not yet honored" in w]
        assert len(offset_warnings) == 1  # deduped


# ===========================================================================
# Same-pipeline upstream is NOT in the graph (ADR #88 — DAG handles it)
# ===========================================================================

class TestSamePipelineExcluded:
    def test_no_upstream_edges_means_only_target(self):
        # caller excludes same-pipeline edges; a target with no cross-pipeline
        # upstream resolves to just its own partitions
        g = AssetGraph()
        g.add_node(AssetNode("solo", "p_solo", "daily"))
        plan = resolve_plan("solo", ["2026-05-20"], g, NONE_EXIST)
        assert len(plan.all_items) == 1
        assert plan.all_items[0].asset == "solo"
