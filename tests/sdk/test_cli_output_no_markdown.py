"""CLI output is plain terminal text — no Markdown backticks.

A terminal prints `polyris-destroy` literally, backticks included (spotted in
the ADR #131 smoke). Commands in CLI messages are highlighted with
``polyris._ansi.c(cmd, CYAN)`` instead (polyris/CLAUDE.md, CLI colour rule).

Scans every string literal in the modules below except docstrings.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULES = ["polyris/up.py", "polyris/cfn_changeset.py", "polyris/platform.py"]


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


@pytest.mark.parametrize("module", MODULES)
def test_no_backticks_in_cli_strings(module):
    tree = ast.parse((REPO_ROOT / module).read_text())
    docstrings = _docstring_nodes(tree)
    offenders = [
        f"{module}:{node.lineno}: {node.value[:70]!r}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstrings and "`" in node.value
    ]
    assert not offenders, "Markdown backticks in CLI output:\n" + "\n".join(offenders)
