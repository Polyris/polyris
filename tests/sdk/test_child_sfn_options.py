"""
SFN child helper contract tests — pin behavior for backfill options
that flow through to runtime execution.

These exist because the external code reviewer correctly identified
that several API options (task_subset/skip_tasks, force, cascade='all',
cascade='none') were accepted at the API but silently ignored at
runtime. ADR #59 documents the resolution: ship them where they
belong, no-op them where they're redundant.

These tests pin which options are actually wired through, so a future
refactor can't silently break the contract again.
"""

import json
import re
from pathlib import Path

import pytest

RUN_TASK = Path(__file__).parent.parent.parent / "sam" / "sfn_templates" / "helpers" / "run_task" / "sfn.tpl.json"
NOTIFY_CONSUMERS = Path(__file__).parent.parent.parent / "sam" / "sfn_templates" / "helpers" / "notify_asset_consumers" / "sfn.tpl.json"


@pytest.fixture(scope="module")
def run_task_template() -> dict:
    """Load run_task template. SAM placeholders like ${name} are not valid
    JSON literals, so we substitute them with dummy values before parsing.
    Two cases:
      - Inside quoted strings: "${name}" → just replace the ${...} with text
      - In numeric position: ${name} (no quotes) → replace with 0
    """
    assert RUN_TASK.exists(), f"Missing: {RUN_TASK}"
    text = RUN_TASK.read_text()
    # Numeric-context placeholders (not inside quotes) → 0
    text = re.sub(r'(?<!")\$\{[a-zA-Z_][a-zA-Z_0-9]*\}(?!")', '0', text)
    # String-context placeholders (inside quotes) → __placeholder__ text
    text = re.sub(r'\$\{[a-zA-Z_][a-zA-Z_0-9]*\}', '__placeholder__', text)
    return json.loads(text)


@pytest.fixture(scope="module")
def notify_consumers_template() -> dict:
    assert NOTIFY_CONSUMERS.exists(), f"Missing: {NOTIFY_CONSUMERS}"
    text = NOTIFY_CONSUMERS.read_text()
    text = re.sub(r'(?<!")\$\{[a-zA-Z_][a-zA-Z_0-9]*\}(?!")', '0', text)
    text = re.sub(r'\$\{[a-zA-Z_][a-zA-Z_0-9]*\}', '__placeholder__', text)
    return json.loads(text)


# ───────────────────────────────────────────────────────────────────────────
# skip_tasks contract (task_subset feature)
# ───────────────────────────────────────────────────────────────────────────


def test_skip_tasks_check_state_exists(run_task_template):
    """run_task must check skip_tasks list before doing any real work.

    Without this, options.tasks (subset backfill) would be silently
    ignored — child execution would run every task regardless of subset.
    """
    states = run_task_template["States"]
    assert "Check_Should_Skip_Task" in states, (
        "Missing Check_Should_Skip_Task — task_subset backfill is broken."
    )
    assert "Task_Skipped" in states, "Missing Task_Skipped Pass state."


def test_skip_tasks_is_start_state(run_task_template):
    """Check_Should_Skip_Task must be the first state so we don't
    waste DDB writes / SFN transitions on tasks that should be skipped."""
    assert run_task_template["StartAt"] == "Check_Should_Skip_Task", (
        f"Wrong StartAt; should skip first: got {run_task_template['StartAt']!r}"
    )


def test_skip_tasks_choice_reads_correct_field(run_task_template):
    """The Choice must read input.skip_tasks (array) and check membership
    with input.task_name (string) using the JSONata `in` operator.

    v0.80.1 (ADR #85): previously used $contains(skip_tasks, task_name),
    but $contains is a string function — passing the skip_tasks array threw
    States.QueryEvaluationError on any non-empty list. Array membership must
    use `in`, matching the dependency_wrapper template.
    """
    state = run_task_template["States"]["Check_Should_Skip_Task"]
    condition = state["Choices"][0]["Condition"]
    assert "skip_tasks" in condition
    assert "task_name" in condition
    assert "in $states.input.skip_tasks" in condition, (
        f"Choice must use the `in` operator for array membership: {condition}"
    )
    assert "$contains" not in condition, (
        f"$contains is a string function — must not wrap the skip_tasks "
        f"array (use `in`): {condition}"
    )


def test_skip_tasks_safe_default(run_task_template):
    """If skip_tasks is missing/empty, default branch routes to existing
    flow. This is the backwards-compatibility guarantee — scheduled
    runs without backfill input behave identically as before."""
    state = run_task_template["States"]["Check_Should_Skip_Task"]
    assert state["Default"] == "Check_Execution_Paused", (
        "Default must route to existing Check_Execution_Paused flow."
    )


def test_task_skipped_emits_skipped_status(run_task_template):
    """Task_Skipped Pass state must emit status='skipped' so downstream
    tasks with trigger_rule 'all_done' proceed correctly. Without this,
    skipped tasks would block the rest of the DAG."""
    state = run_task_template["States"]["Task_Skipped"]
    assert state["Type"] == "Pass"
    assert "'skipped'" in state["Output"]


# ───────────────────────────────────────────────────────────────────────────
# _suppress_asset_event contract (cascade='none' feature)
# ───────────────────────────────────────────────────────────────────────────


def test_suppress_asset_event_check_exists(run_task_template):
    """Check_Has_Outlets must consider _suppress_asset_event flag, not
    just whether outlets exist. Without this, cascade='none' lies —
    asset events fire and downstream consumers wake up."""
    state = run_task_template["States"]["Check_Has_Outlets"]
    condition = state["Choices"][0]["Condition"]
    assert "_suppress_asset_event" in condition, (
        f"Check_Has_Outlets must read _suppress_asset_event: {condition}"
    )


def test_suppress_default_emits_events(run_task_template):
    """Default behavior (no _suppress_asset_event flag) must still
    emit asset events. Otherwise the entire system breaks for normal
    scheduled runs."""
    state = run_task_template["States"]["Check_Has_Outlets"]
    condition = state["Choices"][0]["Condition"]
    # When flag is absent, the condition resolves to true (still emit)
    # via the `? : true` fallback
    assert ": true" in condition or "true %}" in condition, (
        f"Default-emit semantics broken: {condition}"
    )


# ───────────────────────────────────────────────────────────────────────────
# cascade_all contract (cascade='all' feature)
# ───────────────────────────────────────────────────────────────────────────


def test_cascade_all_propagated_to_notify_consumers(run_task_template):
    """run_task must include cascade_all in the input it passes to
    notify_asset_consumers SFN. Without this, cascade='all' is silently
    ignored at the consumer-trigger boundary."""
    # Find the Notify_Asset_Consumers_SFN state inside Emit_Asset_Events Map
    emit = run_task_template["States"]["Emit_Asset_Events"]
    consumer_state = emit["ItemProcessor"]["States"]["Notify_Asset_Consumers_SFN"]
    sfn_input_expr = consumer_state["Arguments"]["Input"]
    assert "cascade_all" in sfn_input_expr, (
        f"cascade_all must be passed to notify_asset_consumers: {sfn_input_expr}"
    )


def test_cascade_all_choice_state_in_notify_consumers(notify_consumers_template):
    """notify_asset_consumers helper must have a Check_Cascade_All state
    that bypasses the operator gate (OR/AND) when cascade_all=true."""
    # The states live inside Map ItemProcessor → walk into it
    # Find the Map state at top level
    states = notify_consumers_template["States"]
    map_state = None
    for s in states.values():
        if s.get("Type") == "Map":
            map_state = s
            break
    assert map_state is not None, "Expected a Map state in notify_asset_consumers"
    item_states = map_state["ItemProcessor"]["States"]
    assert "Check_Cascade_All" in item_states, (
        f"Missing Check_Cascade_All state; got: {list(item_states.keys())}"
    )


def test_cascade_all_bypasses_to_trigger_or(notify_consumers_template):
    """When cascade_all=true, jump directly to Trigger_OR (immediate
    trigger), bypassing the operator/required-assets logic."""
    states = notify_consumers_template["States"]
    map_state = next(s for s in states.values() if s.get("Type") == "Map")
    cascade_state = map_state["ItemProcessor"]["States"]["Check_Cascade_All"]
    assert cascade_state["Choices"][0]["Next"] == "Trigger_OR", (
        f"cascade_all=true must route to Trigger_OR: {cascade_state['Choices']}"
    )


def test_cascade_all_safe_default(notify_consumers_template):
    """If cascade_all is missing/false, default branch preserves
    existing operator-based gating. Backwards-compat for scheduled
    runs and cascade='auto'."""
    states = notify_consumers_template["States"]
    map_state = next(s for s in states.values() if s.get("Type") == "Map")
    cascade_state = map_state["ItemProcessor"]["States"]["Check_Cascade_All"]
    assert cascade_state["Default"] == "Check_Operator"


def test_check_self_trigger_routes_to_cascade_all(notify_consumers_template):
    """Check_Cascade_All must be inserted between Check_Self_Trigger
    (cycle prevention) and Check_Operator. Cycle prevention must still
    run first — cascade_all must not bypass it."""
    states = notify_consumers_template["States"]
    map_state = next(s for s in states.values() if s.get("Type") == "Map")
    self_trigger = map_state["ItemProcessor"]["States"]["Check_Self_Trigger"]
    assert self_trigger["Default"] == "Check_Cascade_All", (
        f"Check_Self_Trigger default should now be Check_Cascade_All: "
        f"{self_trigger['Default']!r}"
    )


# ───────────────────────────────────────────────────────────────────────────
# force / incremental — documented no-ops (ADR #51 deferral, ADR #59)
# ───────────────────────────────────────────────────────────────────────────


def test_force_is_no_op_in_run_task(run_task_template):
    """Per ADR #51 deferral / ADR #59: `force` option is accepted at API
    but is a no-op at runtime — backfill semantics already bypass
    scheduled-run dependency waits, so force is redundant. This test
    pins that no logic reads it (so we don't accidentally couple to it)."""
    template_str = RUN_TASK.read_text()
    # No Choice or Condition referencing $states.input.force should exist
    force_refs = re.findall(r"\$states\.input\.force\b", template_str)
    assert len(force_refs) == 0, (
        f"force is documented as no-op (ADR #59) but template references it: "
        f"{force_refs}. Either implement properly via ADR or remove."
    )


def test_incremental_is_no_op(run_task_template):
    """Same as force — kept in API schema for caller compat, no-op
    at runtime per ADR #51."""
    template_str = RUN_TASK.read_text()
    incr_refs = re.findall(r"\$states\.input\.incremental\b", template_str)
    assert len(incr_refs) == 0, (
        f"incremental must remain a no-op (ADR #51): {incr_refs}"
    )
