"""Coordination wake-up paths log failures at ERROR, never WARN (CLAUDE.md, Error Visibility).

A swallowed WARN on a wake-up path hid the historical `wait_for`
subscription-table mismatch for months. The reconciler and the asset fan-out
are wake-up paths (ADR #130): a failure there delays a task until the
sweeper's next check, and it must show as an error. Two WARN lines are not a
failed wake-up: pointer cleanup after an accepted signal (a leftover pointer
is a harmless no-op, Principle #40) and the note that the time budget ran out
before the remaining records are redelivered.
"""

import ast
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

COORDINATION = Path(__file__).parent.parent.parent / "sam" / "lambdas" / "coordination"
WAKE_UP_MODULES = ("reconcile.py", "asset_fanout.py")

#: (module, function) allowed to log at WARN, with the reason.
WARN_ALLOWED = {
    ("reconcile.py", "_delete_pointers"): "cleanup after an accepted signal; a leftover pointer is a no-op",
    ("asset_fanout.py", "handle"): "the time budget ran out; the records are redelivered, nothing failed",
}


class _LogCalls(ast.NodeVisitor):
    """(innermost function, level, line) of every `log.<level>(...)` call."""

    def __init__(self) -> None:
        self.function: Optional[str] = None
        self.calls: List[Tuple[Optional[str], str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        outer, self.function = self.function, node.name
        self.generic_visit(node)
        self.function = outer

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "log":
            self.calls.append((self.function, func.attr, node.lineno))
        self.generic_visit(node)


def _log_calls(module: str) -> List[Tuple[Optional[str], str, int]]:
    visitor = _LogCalls()
    visitor.visit(ast.parse((COORDINATION / module).read_text()))
    return visitor.calls


@pytest.mark.parametrize("module", WAKE_UP_MODULES)
def test_wake_up_failures_log_at_error(module):
    calls = _log_calls(module)
    assert calls, f"{module} logs nothing; the scan is broken"
    warns = [(function, line) for function, level, line in calls
             if level in ("warn", "warning") and (module, function) not in WARN_ALLOWED]
    assert warns == [], f"{module} logs at WARN on a wake-up path (function, line): {warns}"


@pytest.mark.parametrize("module,function", sorted(WARN_ALLOWED))
def test_warn_allowance_is_still_used(module, function):
    levels = {level for name, level, _ in _log_calls(module) if name == function}
    assert "warn" in levels, f"drop the stale allowance for {module}:{function}"
