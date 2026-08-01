"""
Contract tests for the registration_helper SFN template.

The registration helper resolves the "all deps already terminal at registration
time, trigger rule cannot fire" edge case (bug #1) by extending Eval_Task_Deps
with a $verdict field and routing skip/blocked outcomes through their own
Signal + Update pair, instead of falling to Wait_For_Signal (which would hang
until orchestration_timeout because notify_dependents already fired for those
terminal upstreams and will never fire again).

These tests pin the structure so a future edit can't silently regress the fix
by dropping the branch, the payload shape, or the dynamic status write.
"""

import json
import re
from pathlib import Path

import pytest

TEMPLATE_PATH = (
    Path(__file__).parent.parent.parent
    / "sam" / "sfn_templates" / "helpers" / "registration" / "sfn.tpl.json"
)


@pytest.fixture(scope="module")
def template() -> dict:
    raw = TEMPLATE_PATH.read_text()
    clean = re.sub(r"\$\{[^}]+\}", "0", raw)
    return json.loads(clean)


def test_route_combined_check_has_skip_blocked_branch(template):
    """Route_Combined_Check must route verdict='skip' and verdict='blocked' to
    Signal_Deps_Not_Ready. Without this branch, the task falls to
    Wait_For_Signal and hangs forever (notify_dependents already fired for the
    terminal upstreams and will never fire again)."""
    route = template["States"]["Route_Combined_Check"]
    assert route["Type"] == "Choice"
    conditions = [c["Condition"] for c in route["Choices"]]
    # Combined skip+blocked branch exists
    skip_branch = [c for c in route["Choices"] if "task_verdict" in c["Condition"]]
    assert skip_branch, (
        f"Route_Combined_Check must have a branch on task_verdict "
        f"(skip/blocked); got Choices: {conditions}"
    )
    assert skip_branch[0]["Next"] == "Signal_Deps_Not_Ready"
    assert "'skip'" in skip_branch[0]["Condition"]
    assert "'blocked'" in skip_branch[0]["Condition"]


def test_skip_blocked_branch_ignores_asset_readiness(template):
    """The skip/blocked branch must NOT require asset_deps_ready=true. A task
    whose task_deps cannot be satisfied can never succeed regardless of assets,
    so waiting for assets is wasted time — and worse, if the asset then
    arrives, check_assets signals deps_ready and the wrapper tries to run a
    task with genuinely blocked task_deps. Skipping early on task-deps-blocked
    dodges that entire class of bug."""
    route = template["States"]["Route_Combined_Check"]
    skip_branch = [c for c in route["Choices"] if "task_verdict" in c["Condition"]][0]
    assert "asset_deps_ready" not in skip_branch["Condition"], (
        "Skip/blocked branch must NOT gate on asset_deps_ready; a blocked task "
        f"has to be resolved regardless of assets. Got: {skip_branch['Condition']}"
    )


def test_signal_deps_not_ready_sends_dynamic_signal(template):
    """Signal_Deps_Not_Ready must send a JSONata-computed signal payload with
    'deps_skip' or 'deps_blocked' — matching what dependency_wrapper's
    Wait_For_Dependencies extracts into $callback_signal for routing to
    Emit_Deps_Skip / Emit_Deps_Blocked."""
    state = template["States"]["Signal_Deps_Not_Ready"]
    assert state["Resource"] == "arn:aws:states:::aws-sdk:sfn:sendTaskSuccess"
    assert state["Arguments"]["TaskToken"] == "{% $states.input.wait_token %}"
    output = state["Arguments"]["Output"]
    # Dynamic signal — must reference task_verdict, not a hardcoded 'deps_ready'
    assert "task_verdict" in output, (
        f"Signal_Deps_Not_Ready must build the signal from task_verdict; got: {output}"
    )
    assert "'deps_ready'" not in output, (
        "Signal_Deps_Not_Ready must NOT hardcode deps_ready — that's what "
        "Signal_Ready_Immediately is for."
    )


def test_signal_deps_not_ready_catches_token_errors(template):
    """A sendTaskSuccess on an expired/stale token must fall through to the
    status write, not fault the whole registration SFN. Mirrors
    Signal_Ready_Immediately's catch."""
    state = template["States"]["Signal_Deps_Not_Ready"]
    catches = state.get("Catch", [])
    assert catches, "Signal_Deps_Not_Ready must have a Catch for token errors"
    assert catches[0]["Next"] == "Update_Status_Not_Ready"


def test_update_status_not_ready_writes_dynamic_status(template):
    """Update_Status_Not_Ready must write 'skipped' for skip verdict and
    'upstream_failed' for blocked verdict — a hardcoded value would silently
    mis-report one of the two outcomes."""
    state = template["States"]["Update_Status_Not_Ready"]
    assert state["Resource"] == "arn:aws:states:::dynamodb:updateItem"
    status_val = state["Arguments"]["ExpressionAttributeValues"][":status"]["S"]
    assert "task_verdict" in status_val, (
        f"Update_Status_Not_Ready :status must be computed from task_verdict; got: {status_val}"
    )
    assert "'skipped'" in status_val
    assert "'upstream_failed'" in status_val


def test_eval_task_deps_carries_removed_rule_aliases(template):
    """The JSONata $ready computation must map the 3 removed alias rules
    (none_failed, one_done, none_failed_min_one_success) to their canonical
    equivalents, mirroring evaluate_deps/index.py. Without this, a pre-trim
    pipeline hitting the registration fast-path with `none_failed` would fall
    through to all_success semantics and diverge from the Python-side verdict
    (which now aliases none_failed → all_done via Fix #3)."""
    branches = template["States"]["Check_Dependencies_And_Assets"]["Branches"]
    expr = branches[0]["States"]["Eval_Task_Deps"]["Output"]
    for alias in ("none_failed", "one_done", "none_failed_min_one_success"):
        assert alias in expr, (
            f"Alias {alias!r} missing from Eval_Task_Deps JSONata — pre-trim "
            f"pipelines will diverge from the Python-side semantics"
        )


def test_eval_task_deps_emits_verdict_field(template):
    """The JSONata in Eval_Task_Deps must emit a 'verdict' field alongside
    'ready' — the routing above reads it via task_verdict. If verdict is
    dropped, Route_Combined_Check falls to Wait_For_Signal even in the
    edge case (bug #1 regression)."""
    branches = template["States"]["Check_Dependencies_And_Assets"]["Branches"]
    task_deps_branch = branches[0]  # first branch is task deps
    eval_state = task_deps_branch["States"]["Eval_Task_Deps"]
    output_expr = eval_state["Output"]
    assert "'verdict'" in output_expr, (
        "Eval_Task_Deps must emit a 'verdict' field for the skip/blocked "
        f"routing path; got: {output_expr[:200]}..."
    )
    assert "$verdict" in output_expr


def test_parallel_output_propagates_task_verdict(template):
    """The Parallel state's Output must lift task_verdict from the task-deps
    branch result into the top-level state so Route_Combined_Check can read
    it. Missing this = the whole verdict computation is dead code."""
    parallel = template["States"]["Check_Dependencies_And_Assets"]
    output_expr = parallel["Output"]
    assert "task_verdict" in output_expr, (
        f"Parallel Output must lift task_verdict; got: {output_expr}"
    )


def test_no_orphan_states(template):
    """All declared states must be reachable from StartAt. Catches typos in
    Next/Default that would create dead code or unreachable branches."""
    states = template["States"]
    reachable = set()

    def walk(name):
        if name in reachable or name not in states:
            return
        reachable.add(name)
        s = states[name]
        for k in ("Next", "Default"):
            if k in s and isinstance(s[k], str):
                walk(s[k])
        for c in s.get("Choices", []):
            if isinstance(c.get("Next"), str):
                walk(c["Next"])
        for c in s.get("Catch", []):
            if isinstance(c.get("Next"), str):
                walk(c["Next"])

    walk(template["StartAt"])
    orphans = set(states.keys()) - reachable
    assert not orphans, f"Unreachable top-level states: {orphans}"


# ---------------------------------------------------------------------------
# End-to-end JSONata evaluation — verifies the $verdict expression itself
# returns the right value for each (rule, dep_statuses) combination.
# Skipped when jsonata-python isn't installed; the structural tests above
# still catch the most common regressions.
# ---------------------------------------------------------------------------


def _run_eval_task_deps(template, dep_statuses, trigger_rule, skip_origins=None):
    """Evaluate Eval_Task_Deps' JSONata against a synthetic dep_results list."""
    jsonata = pytest.importorskip("jsonata")
    branches = template["States"]["Check_Dependencies_And_Assets"]["Branches"]
    expr_body = branches[0]["States"]["Eval_Task_Deps"]["Output"]
    expr = expr_body[2:-2].strip()
    origins = skip_origins or [""] * len(dep_statuses)
    dep_results = [
        {"dep": f"d{i}", "status": s, "skip_origin": o}
        for i, (s, o) in enumerate(zip(dep_statuses, origins))
    ]
    j = jsonata.Jsonata(expr)
    j.assign("states", {"input": {"dep_results": dep_results, "trigger_rule": trigger_rule}})
    return j.evaluate({})


@pytest.mark.parametrize("rule,statuses,expected_verdict", [
    ("all_success", ["success", "success"], "ready"),
    ("all_success", ["success", "failed"], "blocked"),        # failure_averse + failed
    ("all_success", ["success", "waiting"], "wait"),          # still pending
    ("all_success", ["skipped", "skipped"], "ready"),         # skip_origin="" → not rule-originated → treated as OK
    ("one_success", ["failed", "success"], "ready"),
    ("one_success", ["failed", "failed"], "blocked"),         # failure_averse + all failed
    ("one_success", ["skipped", "skipped"], "skip"),          # no success, no failure
    ("all_done", ["success", "failed"], "ready"),             # all_done fires anyway
    ("all_done", ["success", "waiting"], "wait"),
    ("all_skipped", ["skipped", "skipped"], "ready"),
    ("all_skipped", ["skipped", "success"], "skip"),          # not failure_averse, no failures
    ("none_skipped", ["success", "success"], "ready"),
    ("none_skipped", ["success", "skipped"], "skip"),         # skip present, not failure_averse
    # Removed-rule aliases must match their canonical target's semantics
    ("none_failed", ["success", "failed"], "ready"),          # aliased to all_done → fires
    ("none_failed", ["success", "waiting"], "wait"),          # aliased to all_done → waits
    ("one_done", ["success", "failed"], "ready"),             # aliased to all_done
    ("none_failed_min_one_success", ["success", "failed"], "ready"),  # aliased to one_success
])
def test_verdict_matches_python_semantics(template, rule, statuses, expected_verdict):
    """The JSONata $verdict must produce the same outcome as the Python-side
    verdict logic in evaluate_deps/index.py — otherwise the registration
    fast-path diverges from the notify_dependents path for the same inputs."""
    result = _run_eval_task_deps(template, statuses, rule)
    assert result["verdict"] == expected_verdict, (
        f"rule={rule} statuses={statuses}: expected verdict={expected_verdict!r}, "
        f"got {result['verdict']!r} (ready={result['ready']}, counts={result['counts']})"
    )
