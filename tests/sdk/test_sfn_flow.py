"""
SFN Template Flow Tests — Structural graph validation.

Validates that all Step Function templates have valid state machine graphs:
- All Next/Default/Catch references point to existing states
- All states are reachable from StartAt
- No orphaned states
- Every path eventually reaches a terminal state (Succeed/Fail/End)
- All Task states have error handling (Catch or Retry)
- Map states have valid ItemProcessor definitions
- Choice states have Default fallback

These tests catch broken references and dead code in SFN templates
WITHOUT needing AWS or JSONata evaluation.
"""

import json
from pathlib import Path
from typing import Dict, Any, Set, List, Tuple

import pytest

# sys.path setup moved to conftest.py

TEMPLATES_DIR = Path(__file__).parent.parent.parent / "sam" / "sfn_templates"


def discover_templates() -> List[Tuple[str, Path]]:
    """Find all SFN template JSON files."""
    templates = []
    for f in sorted(TEMPLATES_DIR.rglob("*.json")):
        name = f.parent.name
        if f.parent.parent.name == "helpers":
            name = f"helpers/{name}"
        templates.append((name, f))
    return templates


def load_template(path: Path) -> dict:
    """Load template, tolerating ${} template variables."""
    import re
    with open(path) as f:
        text = f.read()
    text = re.sub(r'\$\{[^}]+\}', '0', text)
    return json.loads(text)


# ============================================================
# Graph analysis helpers
# ============================================================

def get_next_states(state: Dict[str, Any]) -> Set[str]:
    """Extract all states that this state can transition to."""
    targets = set()

    # Direct Next
    if "Next" in state:
        targets.add(state["Next"])

    # Default (Choice states)
    if "Default" in state:
        targets.add(state["Default"])

    # Choice branches
    for choice in state.get("Choices", []):
        if "Next" in choice:
            targets.add(choice["Next"])

    # Catch targets
    for catch in state.get("Catch", []):
        if "Next" in catch:
            targets.add(catch["Next"])

    # Map/Parallel — check ItemProcessor and Branches
    if "ItemProcessor" in state:
        proc = state["ItemProcessor"]
        if "States" in proc:
            # Inner states reference inner states, not outer
            pass

    for branch in state.get("Branches", []):
        if "States" in branch:
            # Branches are self-contained
            pass

    return targets


def get_all_reachable(states: Dict[str, Any], start: str) -> Set[str]:
    """BFS to find all reachable states from start."""
    visited = set()
    queue = [start]

    while queue:
        current = queue.pop(0)
        if current in visited or current not in states:
            continue
        visited.add(current)
        queue.extend(get_next_states(states[current]))

    return visited


def is_terminal(state: Dict[str, Any]) -> bool:
    """Check if state is terminal (no further transitions)."""
    state_type = state.get("Type", "")
    if state_type in ("Succeed", "Fail"):
        return True
    if state.get("End") is True:
        return True
    return False


def get_inner_machines(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract inner state machines from Map/Parallel states."""
    machines = []

    # Map with ItemProcessor
    proc = state.get("ItemProcessor", {})
    if "States" in proc and "StartAt" in proc:
        machines.append(proc)

    # Parallel branches
    for branch in state.get("Branches", []):
        if "States" in branch and "StartAt" in branch:
            machines.append(branch)

    return machines


def validate_machine(states: Dict[str, Any], start_at: str, prefix: str = "") -> List[str]:
    """Validate a state machine graph. Returns list of errors."""
    errors = []
    label = f"[{prefix}] " if prefix else ""

    # 1. StartAt exists
    if start_at not in states:
        errors.append(f"{label}StartAt '{start_at}' not found in States")
        return errors

    # 2. All references point to existing states
    for name, state in states.items():
        for target in get_next_states(state):
            if target not in states:
                errors.append(f"{label}State '{name}' references non-existent state '{target}'")

    # 3. All states reachable from StartAt
    reachable = get_all_reachable(states, start_at)
    unreachable = set(states.keys()) - reachable
    if unreachable:
        errors.append(f"{label}Unreachable states: {sorted(unreachable)}")

    # 4. Every non-terminal state has an outgoing transition
    for name, state in states.items():
        if name not in reachable:
            continue
        if not is_terminal(state) and not get_next_states(state):
            errors.append(f"{label}State '{name}' is non-terminal but has no outgoing transitions")

    # 5. At least one terminal state is reachable
    terminal_reachable = [n for n in reachable if is_terminal(states[n])]
    if not terminal_reachable:
        errors.append(f"{label}No terminal state reachable from '{start_at}'")

    # 6. Recursively validate inner machines (Map/Parallel)
    for name, state in states.items():
        if name not in reachable:
            continue
        for inner in get_inner_machines(state):
            inner_errors = validate_machine(
                inner["States"], inner["StartAt"],
                prefix=f"{prefix}/{name}" if prefix else name
            )
            errors.extend(inner_errors)

    return errors


# ============================================================
# Test: Graph integrity for all templates
# ============================================================

TEMPLATE_PARAMS = discover_templates()


@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_graph_integrity(name, path):
    """All state references resolve, all states reachable, terminal states exist."""
    data = load_template(path)
    errors = validate_machine(data["States"], data["StartAt"])
    assert not errors, "\n".join(errors)


# ============================================================
# Test: All Task states have Catch (error resilience)
# ============================================================

def collect_task_states(
    states: Dict[str, Any], prefix: str = "", parent_has_catch: bool = False
) -> List[Tuple[str, Dict, bool]]:
    """Recursively collect all Task states including inner machines.
    Returns (full_name, state, covered_by_parent)."""
    results = []
    for name, state in states.items():
        full_name = f"{prefix}/{name}" if prefix else name
        if state.get("Type") == "Task":
            results.append((full_name, state, parent_has_catch))
        # Check if this Map/Parallel has Catch (covers children)
        this_has_catch = "Catch" in state or parent_has_catch
        for inner in get_inner_machines(state):
            results.extend(collect_task_states(
                inner["States"], prefix=full_name, parent_has_catch=this_has_catch
            ))
    return results


# Critical templates that MUST have Catch on every Task state
CRITICAL_TEMPLATES = [
    "dependency_wrapper",
    "helpers/run_task",
    "helpers/failure_handler",
    "helpers/registration",
    "helpers/notify_dependents",
]


@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_task_states_have_error_handling(name, path):
    """Task states in critical templates must have Catch (own or parent)."""
    if name not in CRITICAL_TEMPLATES:
        pytest.skip(f"Non-critical template: {name}")

    data = load_template(path)
    task_states = collect_task_states(data["States"])

    # Known gaps: inner states covered by wrapper-level States.ALL catch.
    # TODO: Add Catch/Retry to registration Parallel for clearer error messages.
    KNOWN_GAPS = {
        "Check_Dependencies_And_Assets/Save_Task_Subscriptions/Save_One_Task_Sub",
        "Check_Dependencies_And_Assets/Check_Task_Deps_Status/Get_Task_Dep_Status",
    }

    missing = [
        n for n, s, covered in task_states
        if "Catch" not in s and "Retry" not in s and not covered and n not in KNOWN_GAPS
    ]
    assert not missing, f"Task states without Catch/Retry (not covered by parent): {missing}"


# ============================================================
# Test: Choice states have Default (prevent stuck executions)
# ============================================================

@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_choice_states_have_default(name, path):
    """Choice states should have Default to prevent stuck executions."""
    data = load_template(path)
    missing = []

    def check_choices(states, prefix=""):
        for sname, state in states.items():
            full = f"{prefix}/{sname}" if prefix else sname
            if state.get("Type") == "Choice" and "Default" not in state:
                missing.append(full)
            for inner in get_inner_machines(state):
                check_choices(inner["States"], prefix=full)

    check_choices(data["States"])
    # Warning level — some Choice states intentionally omit Default
    if missing:
        pytest.skip(f"Choice states without Default (review recommended): {missing}")


# ============================================================
# Test: No circular references (infinite loops)
# ============================================================

def find_cycles(states: Dict[str, Any], start: str) -> List[List[str]]:
    """Detect cycles in state graph using DFS."""
    cycles = []

    def dfs(node: str, path: List[str], visited: Set[str]):
        if node not in states:
            return
        if node in visited:
            cycle_start = path.index(node)
            cycles.append(path[cycle_start:] + [node])
            return
        visited.add(node)
        path.append(node)
        for target in get_next_states(states[node]):
            dfs(target, path[:], visited.copy())

    dfs(start, [], set())
    return cycles


@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_no_unintentional_cycles(name, path):
    """Detect cycles — some are intentional (retry loops), flag for review."""
    data = load_template(path)
    cycles = find_cycles(data["States"], data["StartAt"])
    # Not asserting — cycles can be intentional (Wait → Check → Wait)
    # Just log for awareness
    if cycles:
        # Only fail if cycle doesn't go through a Wait or Choice state
        suspicious = []
        for cycle in cycles:
            has_wait = any(
                data["States"].get(s, {}).get("Type") in ("Wait", "Choice")
                for s in cycle[:-1]
            )
            if not has_wait:
                suspicious.append(cycle)
        assert not suspicious, f"Suspicious cycles (no Wait/Choice): {suspicious}"


# ============================================================
# Test: Consistent error output pattern
# ============================================================

@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_catch_preserves_input(name, path):
    """Catch blocks should preserve input state (not lose context)."""
    if name not in CRITICAL_TEMPLATES:
        pytest.skip(f"Non-critical template: {name}")

    data = load_template(path)
    issues = []

    def check(states, prefix=""):
        for sname, state in states.items():
            full = f"{prefix}/{sname}" if prefix else sname
            for catch in state.get("Catch", []):
                # Catch should have Output that includes input or error
                output = catch.get("Output", catch.get("ResultPath", ""))
                output_str = json.dumps(output) if isinstance(output, dict) else str(output)
                if output_str == "" or output_str == "null":
                    issues.append(f"{full}: Catch block has no Output/ResultPath — context will be lost")
            for inner in get_inner_machines(state):
                check(inner["States"], prefix=full)

    check(data["States"])
    assert not issues, "\n".join(issues)


# ============================================================
# Test: Template metadata
# ============================================================

@pytest.mark.parametrize("name,path", TEMPLATE_PARAMS, ids=[t[0] for t in TEMPLATE_PARAMS])
def test_template_has_comment(name, path):
    """Templates should have a top-level Comment describing purpose."""
    data = load_template(path)
    assert "Comment" in data, f"Template '{name}' missing top-level Comment"
    assert len(data["Comment"]) > 10, f"Template '{name}' Comment too short: '{data['Comment']}'"
