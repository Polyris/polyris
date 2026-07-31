"""
SFN template contract tests for bulk_backfill.

These exist because unit tests against routes/backfill.py mocked
sfn.start_execution and never verified the actual SFN template's
behavior. The external reviewer correctly identified that:
  - MaxConcurrency was hardcoded to 5 (options.max_parallel ignored)
  - skip_tasks JSONata was broken (both branches returned [])
  - Check_If_Done state did not exist

These tests pin the template's contract so regressions surface in CI
before reaching production.
"""

import json
import re
from pathlib import Path

import pytest

TEMPLATE_PATH = Path(__file__).parent.parent.parent / "sam" / "sfn_templates" / "bulk_backfill" / "sfn.tpl.json"


@pytest.fixture(scope="module")
def template() -> dict:
    """Load the bulk-backfill SFN template once."""
    assert TEMPLATE_PATH.exists(), f"Missing: {TEMPLATE_PATH}"
    return json.loads(TEMPLATE_PATH.read_text())


def _inner_map(template) -> dict:
    """The inner per-partition Map (ADR #90 nested structure)."""
    return template["States"]["TierMap"]["ItemProcessor"]["States"]["PartitionMap"]


def _inner_states(template) -> dict:
    """States of the inner per-partition item processor."""
    return _inner_map(template)["ItemProcessor"]["States"]


def test_template_is_jsonata_mode(template):
    """The whole expression library below assumes JSONata mode."""
    assert template.get("QueryLanguage") == "JSONata"


def test_max_concurrency_is_dynamic_not_hardcoded(template):
    """MaxConcurrency must read from input.options.max_parallel.

    Regression for the bug where MaxConcurrency: 5 was hardcoded,
    silently ignoring user's max_parallel option from the API.
    """
    map_state = _inner_map(template)
    mc = map_state["MaxConcurrency"]
    # If it's an integer, the bug is back.
    assert not isinstance(mc, int), (
        f"MaxConcurrency is hardcoded to {mc}. Must be a JSONata expression "
        "reading from $states.input.options.max_parallel."
    )
    assert isinstance(mc, str)
    assert "$states.input.options.max_parallel" in mc, (
        f"MaxConcurrency expression must reference options.max_parallel; got: {mc}"
    )
    # JSONata expression syntax check
    assert mc.startswith("{%") and mc.endswith("%}"), (
        f"MaxConcurrency must use JSONata expression syntax: {mc}"
    )


def test_skip_tasks_reads_from_input(template):
    """skip_tasks expression must pass through input.skip_task_ids.

    Regression for the bug where both branches of the ternary returned
    [], so task_subset was always silently ignored by child executions.
    """
    map_state = _inner_map(template)
    start_child = map_state["ItemProcessor"]["States"]["StartChildSFN"]
    skip_tasks_expr = start_child["Arguments"]["Input"]["skip_tasks"]
    assert "skip_task_ids" in skip_tasks_expr, (
        f"skip_tasks must reference skip_task_ids from input; got: {skip_tasks_expr}"
    )
    # Critical: must NOT have the broken `? [] : []` pattern.
    assert not re.search(r"\?\s*\[\s*\]\s*:\s*\[\s*\]", skip_tasks_expr), (
        f"skip_tasks has the broken `? [] : []` pattern: {skip_tasks_expr}"
    )


def test_item_selector_propagates_skip_task_ids(template):
    """ItemSelector must carry skip_task_ids into each Map iteration,
    otherwise the child execution can't read it."""
    map_state = _inner_map(template)
    item_selector = map_state["ItemSelector"]
    assert "skip_task_ids" in item_selector, (
        f"ItemSelector must include skip_task_ids; keys: {list(item_selector.keys())}"
    )


def test_cooperative_cancel_state_exists(template):
    """The CheckBackfillCanceled state must exist — this is the cancel
    contract from ADR #54."""
    states = _inner_states(template)
    assert "CheckBackfillCanceled" in states
    assert "IsCanceledChoice" in states
    assert "PartitionSkippedCanceled" in states


def test_no_check_if_done_state(template):
    """v0.78 contract: SFN does NOT have a Check_If_Done state — the
    skip_completed filtering happens at API request time via
    routes/backfill.py::_scan_completed_partitions. If this state ever
    gets added, _scan_completed_partitions must be re-evaluated for
    duplication (CLAUDE.md #1)."""
    states = _inner_states(template)
    assert "Check_If_Done" not in states, (
        "SFN has Check_If_Done — review the duplication with the API-side "
        "_scan_completed_partitions per CLAUDE.md #1."
    )


def test_initialize_marks_running(template):
    """Initialize must flip the backfill record status to running."""
    init = template["States"]["Initialize"]
    args = init["Arguments"]
    assert args["UpdateExpression"] == "SET #status = :running, started_at = :now"
    assert args["ExpressionAttributeValues"][":running"]["S"] == "running"


def test_finalize_aggregates_status(template):
    """Final status computed from counters per ADR #56."""
    finalize = template["States"]["Finalize"]
    expr = finalize["Output"]
    # Must look at completed and failed partition counters
    assert "completed_partitions" in expr
    assert "failed_partitions" in expr
    # Must produce one of the valid final statuses
    for s in ("completed", "failed", "partial", "canceled"):
        assert s in expr, f"final status {s!r} not computed in: {expr}"


def test_start_child_uses_sync2(template):
    """Child execution must use .sync:2 so the Map iteration waits for
    completion before reporting status."""
    start_child = _inner_states(template)["StartChildSFN"]
    assert start_child["Resource"].endswith(".sync:2"), (
        f"StartChildSFN must use startExecution.sync:2 (waits for completion); "
        f"got: {start_child['Resource']}"
    )


def test_all_options_documented_in_comment(template):
    """The inner Map's Comment should call out that concurrency is driven by
    max_parallel. This is a self-check — if someone makes MaxConcurrency
    static again, this comment (and the canary token) should call them out."""
    map_state = _inner_map(template)
    comment = map_state.get("Comment", "")
    assert "max_parallel" in comment, (
        f"Comment should mention max_parallel-driven concurrency: {comment}"
    )


# ── Phase 3: nested tier Map (ADR #90) ───────────────────────────────────

def test_outer_tier_map_is_sequential(template):
    """Outer TierMap must run tiers strictly in order (MaxConcurrency=1),
    so deepest upstream completes before its consumers."""
    tier_map = template["States"]["TierMap"]
    assert tier_map["Type"] == "Map"
    assert tier_map["MaxConcurrency"] == 1
    assert tier_map["Items"] == "{% $states.input.tiers %}"


def test_inner_partition_map_is_parallel(template):
    """Inner PartitionMap keeps per-tier parallelism via max_parallel."""
    inner = _inner_map(template)
    assert inner["Type"] == "Map"
    assert "$states.input.options.max_parallel" in inner["MaxConcurrency"]


def test_tier_gate_blocks_on_failure_or_cancel(template):
    """The tier gate must skip a tier when canceled or a prior tier failed —
    prevents running a target on failed upstream (ADR #90)."""
    tier_states = template["States"]["TierMap"]["ItemProcessor"]["States"]
    assert "CheckTierGate" in tier_states
    choice = tier_states["TierGateChoice"]
    cond = choice["Choices"][0]["Condition"]
    assert "canceled" in cond
    assert "failed_so_far" in cond
    assert "TierSkipped" in tier_states


def test_reused_items_are_skipped(template):
    """A reused upstream partition must not execute (ADR #88/#90)."""
    inner = _inner_states(template)
    assert "CheckReused" in inner
    assert "PartitionReused" in inner
    cond = inner["CheckReused"]["Choices"][0]["Condition"]
    assert "reused" in cond
    # reused leaf must not start a child execution
    assert inner["PartitionReused"]["Type"] == "Pass"


def test_start_child_uses_per_item_pipeline(template):
    """Cross-pipeline: StartChildSFN reads the item's own arn/pipeline."""
    start_child = _inner_states(template)["StartChildSFN"]
    assert start_child["Arguments"]["StateMachineArn"] == "{% $states.input.target_pipeline_sfn_arn %}"
    # the inner ItemSelector sources arn from the item, not a fixed top-level
    sel = _inner_map(template)["ItemSelector"]
    assert "$states.context.Map.Item.Value.sfn_arn" in sel["target_pipeline_sfn_arn"]
    assert "$states.context.Map.Item.Value.pipeline" in sel["target_pipeline"]
