"""No code reads the registry's ``tasks`` or ``dag`` (ADR #137 §8).

The specs live in ``pipeline-specs``, and the console reads them only through
``dal.specs_repo``. A reader of the registry's ``tasks`` or ``dag`` left behind
reads a missing field as empty (``'[]'``, ``'{}'``) and fails silently: a
backfill would run the tasks its user excluded with ``skip_on_backfill``.

This census reads the production code of the SDK, of every Lambda and, when it
is checked out beside this repository or merged into the console, of the paid
overlay. Any read of a key ``tasks`` or ``dag`` (``x['tasks']``,
``x.get('dag')``) fails the test unless ``ALLOWED`` names it with the reason it
is not a read of the registry. ``Register_Pipeline`` still writes both fields,
for the 1.2 engine (Principle #39); a write is not a read.
"""

import ast
from pathlib import Path
from typing import Iterator, Tuple

REPO = Path(__file__).resolve().parents[2]
CONSOLE = REPO / "sam" / "lambdas" / "console_api"
OVERLAYS = (CONSOLE / "ee", REPO.parent / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee")
KEYS = {"tasks", "dag"}

#: (file, enclosing function, key): why each read is not one of the registry's fields.
ALLOWED = {
    # The 1.2 engine's outlet granularity lookup; it goes with the 1.1.4 code in TR-8.
    ("sam/lambdas/coordination/stores.py", "_lookup", "dag"):
        "stores.Registry, deleted in TR-8 with the 1.2 engine",
    # The DAG snapshot of a 1.2 run (dag_snapshot::{run} in pipeline-tokens), kept until 1.4 (D89).
    ("sam/lambdas/console_api/routes/pipelines_info.py", "get_pipeline_dag", "dag"):
        "the 1.2 runs' DAG snapshot reader, removed in 1.4",
    # A dict of this route's own: the tasks of each execution it groups.
    ("sam/lambdas/console_api/routes/pipelines_list.py", "get_pipeline_status", "tasks"):
        "a local dict of execution rows, not the registry",
    # polyris-validate's debug output builds its own list of tasks.
    ("polyris/generators.py", "generate_debug_info", "tasks"): "the debug output's own list",
    # The request body of a backfill: the task subset the user asked for.
    ("ee/team/backfill.py", "start_backfill", "tasks"): "the request body's task subset",
}


def _sources() -> Iterator[Tuple[str, Path]]:
    roots = [REPO / "polyris", REPO / "sam" / "lambdas"]
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if {"tests", "ee", ".aws-sam", "__pycache__"} & set(path.parts) or path.name.startswith("test_"):
                continue
            yield str(path.relative_to(REPO)), path
    overlay = next((o for o in OVERLAYS if o.is_dir()), None)
    if overlay is not None:
        for path in sorted(overlay.rglob("*.py")):
            if "tests" not in path.parts and not path.name.startswith("test_") and "__pycache__" not in path.parts:
                yield "ee/" + str(path.relative_to(overlay)), path


def _key(node: ast.AST) -> str:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else ""


def _reads(tree: ast.AST) -> Iterator[Tuple[str, str, int]]:
    """(enclosing function, key, line) of each read of a key in KEYS."""
    def visit(node: ast.AST, function: str) -> Iterator[Tuple[str, str, int]]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and _key(node.slice) in KEYS:
            yield function, _key(node.slice), node.lineno
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                and node.args and _key(node.args[0]) in KEYS):
            yield function, _key(node.args[0]), node.lineno
        for child in ast.iter_child_nodes(node):
            yield from visit(child, function)
    yield from visit(tree, "")


def _census():
    found = {}
    for name, path in _sources():
        for function, key, line in _reads(ast.parse(path.read_text(), str(path))):
            found.setdefault((name, function, key), []).append(line)
    return found


def test_no_code_reads_the_registrys_tasks_or_dag():
    unexplained = {f"{name}:{lines[0]} {function}() reads {key!r}": None
                   for (name, function, key), lines in _census().items() if (name, function, key) not in ALLOWED}
    assert sorted(unexplained) == [], "read the task specs through dal.specs_repo (ADR #137 §8)"


def test_every_exemption_still_names_a_read():
    """A stale exemption would let the next read in that function through unseen."""
    found = _census()
    overlay_present = any(o.is_dir() for o in OVERLAYS)
    stale = [entry for entry in ALLOWED if entry not in found and (overlay_present or not entry[0].startswith("ee/"))]
    assert stale == []


def test_the_census_sees_a_read():
    tree = ast.parse("def f(item):\n    return item.get('tasks', '[]'), item['dag']\n")
    assert sorted(_reads(tree)) == [("f", "dag", 2), ("f", "tasks", 2)]
