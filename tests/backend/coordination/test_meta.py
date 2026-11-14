"""The harness checks itself (ADR #130, Testing).

- every defect has a scenario, and every scenario names a known defect;
- a defect whose scenarios run has a mutation probe that targets one of them;
- needs_binding markers name known stages, and never a bound one;
- scenarios build state only through the World, never by writing tables.
"""

import ast
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple

from defects import DEFECTS
from probes import PROBES
from stages import BOUND_STAGES, STAGES

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
SCENARIO_FILES = sorted(HERE.glob("test_scenarios*.py"))


def _marks(decorator: ast.expr) -> Tuple[str, str]:
    """('defect', 'B-P1') for @defect("B-P1") or @pytest.mark.defect("B-P1")."""
    if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant):
        func = decorator.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name, str(decorator.args[0].value)
    return "", ""


def _scenarios() -> Dict[str, Dict[str, List[str]]]:
    """{nodeid: {"defect": [...], "needs_binding": [...]}} for every scenario function."""
    found: Dict[str, Dict[str, List[str]]] = {}
    for path in SCENARIO_FILES:
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                marks: Dict[str, List[str]] = {"defect": [], "needs_binding": []}
                for decorator in node.decorator_list:
                    kind, value = _marks(decorator)
                    if kind in marks:
                        marks[kind].append(value)
                found[f"{path.relative_to(REPO_ROOT)}::{node.name}"] = marks
    return found


def test_every_defect_has_a_scenario_and_every_scenario_names_a_known_defect():
    named = {d for marks in _scenarios().values() for d in marks["defect"]}
    assert named - set(DEFECTS) == set(), "scenarios name defects that defects.py does not list"
    assert set(DEFECTS) - named == set(), "defects without a scenario"


def test_needs_binding_markers_name_unbound_stages_only():
    for nodeid, marks in _scenarios().items():
        for stage in marks["needs_binding"]:
            assert stage in STAGES, f"{nodeid}: unknown stage {stage!r}"
            assert stage not in BOUND_STAGES, f"{nodeid}: the {stage} stage is bound; remove the marker"


def test_a_defect_whose_scenarios_run_has_a_probe_on_one_of_them():
    scenarios = _scenarios()
    probed: Dict[str, Set[str]] = {}
    for probe in PROBES:
        probed.setdefault(probe.defect, set()).add(probe.scenario.split("[")[0])
    for defect in DEFECTS:
        running = {n for n, m in scenarios.items() if defect in m["defect"] and not m["needs_binding"]}
        if running:
            assert probed.get(defect, set()) & running, f"{defect}: add a probe in probes.py for one of {sorted(running)}"


def test_probes_apply_to_their_files():
    for probe in PROBES:
        assert probe.defect in DEFECTS, probe
        assert probe.edits, probe
        for edit in probe.edits:
            text = (REPO_ROOT / edit.path).read_text()
            assert text.count(edit.old) == 1, f"{probe.defect}: an edit's text must occur once in {edit.path}"


def test_skipped_scenarios_call_only_verbs_the_world_has():
    from world import Clock, World

    for path in SCENARIO_FILES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "world":
                known = hasattr(World, node.attr) or node.attr in World.__annotations__
                assert known, f"{path.name}:{node.lineno}: World has no {node.attr!r}"
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                    and node.value.attr == "clock"):
                assert hasattr(Clock, node.attr), f"{path.name}:{node.lineno}: Clock has no {node.attr!r}"


def test_scenarios_build_state_only_through_the_world():
    forbidden = re.compile(r"\.(put_item|update_item|delete_item|batch_write_item|transact_write_items)\(|\.ddb\b")
    for path in SCENARIO_FILES:
        for number, line in enumerate(path.read_text().splitlines(), 1):
            assert not forbidden.search(line), f"{path.name}:{number}: write through a World verb, not to a table"
