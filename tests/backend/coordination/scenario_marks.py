"""The marks of the harness's scenarios, read from their source (test_meta.py, run_probes.py).

A scenario's ``@defect(id)`` names the defect or class of bug it pins, and its
``@needs_binding(stage)`` the stage of stages.py whose code it waits for.
"""

import ast
from pathlib import Path
from typing import Dict, List, Tuple

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[2]
SCENARIO_FILES = sorted(HERE.glob("test_scenarios*.py"))


def marks(decorator: ast.expr) -> Tuple[str, str]:
    """('defect', 'B-P1') for @defect("B-P1") or @pytest.mark.defect("B-P1")."""
    if isinstance(decorator, ast.Call) and decorator.args and isinstance(decorator.args[0], ast.Constant):
        func = decorator.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        return name, str(decorator.args[0].value)
    return "", ""


def scenarios() -> Dict[str, Dict[str, List[str]]]:
    """{nodeid: {"defect": [...], "needs_binding": [...]}} for every scenario function."""
    found: Dict[str, Dict[str, List[str]]] = {}
    for path in SCENARIO_FILES:
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                found_marks: Dict[str, List[str]] = {"defect": [], "needs_binding": []}
                for decorator in node.decorator_list:
                    kind, value = marks(decorator)
                    if kind in found_marks:
                        found_marks[kind].append(value)
                found[f"{path.relative_to(REPO_ROOT)}::{node.name}"] = found_marks
    return found
