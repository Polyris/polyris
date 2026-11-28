"""Every ``polyris`` module the repository imports is a file of this checkout.

An editable install of another checkout still supplies a module that a branch removes, so its importers
pass locally and fail in CI (the 1.4.0 review: ``test_small_modules.py`` imported the removed
``polyris.resolver``). This reads the imports instead of running them, so no install can answer for them.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
#: Directories that hold no code of the repository: tools' state, dependencies, builds.
SKIPPED = {".git", ".claude", "node_modules", ".aws-sam", "build", "dist", ".venv", "venv", "__pycache__",
           ".mypy_cache", ".pytest_cache", ".ruff_cache"}
#: Modules a guarded import may lack: the paid overlay ships them (Principle #44).
OPTIONAL = ("polyris._ee",)


def _sources() -> Iterator[Path]:
    for path in REPO_ROOT.rglob("*.py"):
        if not SKIPPED.intersection(path.relative_to(REPO_ROOT).parts):
            yield path


def _imported(path: Path) -> Iterator[Tuple[int, str]]:
    for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def _exists(module: str) -> bool:
    base = REPO_ROOT.joinpath(*module.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def test_every_polyris_module_imported_is_a_file_of_this_checkout():
    missing = sorted(f"{path.relative_to(REPO_ROOT)}:{line}: {module}"
                     for path in _sources() for line, module in _imported(path)
                     if (module == "polyris" or module.startswith("polyris."))
                     and not module.startswith(OPTIONAL) and not _exists(module))
    assert missing == []


def test_the_check_sees_a_removed_module():
    """The scan reads this repository's files: a module no file holds is missing."""
    assert _exists("polyris.platform") and not _exists("polyris.resolver")
    assert any(path.name == "test_small_modules.py" for path in _sources())
