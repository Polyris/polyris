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


# ───────────────────────────────────────────────────────────────────────────
# skip_tasks contract (task_subset feature)
# ───────────────────────────────────────────────────────────────────────────


def test_a_task_in_skip_tasks_never_reaches_run_task(run_task_template):
    """1.3 (ADR #137 §3): Start_Run writes a task named in the run's skip_tasks as skipped, with
    skip_origin = run, so no run_task starts for it and run_task checks no skip list
    (test_coordination_start_run.py covers the write)."""
    assert "skip_tasks" not in json.dumps(run_task_template)
    assert run_task_template["StartAt"] == "Claim"


# ───────────────────────────────────────────────────────────────────────────
# _suppress_asset_event contract (cascade='none' feature)
# ───────────────────────────────────────────────────────────────────────────


# cascade='none' (from _suppress_asset_event) is evaluated, not string-matched,
# in test_run_task_facts.py: the fact is recorded with cascade 'none' and starts
# no push consumer (ADR #130 §6.1).


# ───────────────────────────────────────────────────────────────────────────
# cascade_all contract (cascade='all' feature)
# ───────────────────────────────────────────────────────────────────────────


def test_cascade_all_is_recorded_on_the_fact(run_task_template):
    """cascade='all' reaches the push consumers through the fact itself: AssetFanout
    starts them regardless of their schedule (ADR #130 §6.1). Start_Run keeps the run's
    cascade (records.cascade_of) and the dispatcher passes it on; here, that the facts of
    the success transaction carry it (test_run_task_facts.py evaluates them)."""
    record = run_task_template["States"]["Record_Success"]["Arguments"]
    assert "'cascade': {'S': $states.context.Execution.Input.cascade}" in record


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
