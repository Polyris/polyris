"""Every `polyris-<cmd>` in a shell-invocation position must be a real
entry point declared in `pyproject.toml [project.scripts]`.

Rejects `polyris-nonexistent` docstrings that ship with the docs but
have no console script backing them.
"""
from __future__ import annotations

import re
import tomllib

import pytest

from tests.docs._helpers import (
    REPO_ROOT,
    format_findings,
    iter_docs,
    rel,
)


PYPROJECT = REPO_ROOT / "pyproject.toml"

# Match `polyris-<cmd>` only at start of a shell-invocation line — leading
# whitespace / `$` / `>` prompt. This filters out prose mentions and AWS
# resource names (`polyris-console-api`, `polyris-default-task-role`) that
# happen to share the prefix.
_ENTRY_POINT_RE = re.compile(r"^[\s$>]*(polyris-[a-z-]+)(?=\s|$)", re.MULTILINE)


def _load_entry_points() -> set[str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return set(data.get("project", {}).get("scripts", {}).keys())


def test_entry_points_referenced_in_docs_exist() -> None:
    known = _load_entry_points()
    findings: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str]] = set()
    for path in iter_docs():
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        for m in _ENTRY_POINT_RE.finditer(text):
            cmd = m.group(1)
            if cmd in known:
                continue
            pos = m.start()
            lineno = next(
                (i for i, s in enumerate(line_starts) if s > pos), len(line_starts)
            )
            key = (rel(path), lineno, cmd)
            if key in seen:
                continue
            seen.add(key)
            findings.append((
                rel(path),
                lineno,
                f"unknown entry point {cmd!r} — available: {', '.join(sorted(known))}",
            ))
    if findings:
        pytest.fail(format_findings("entry_points", findings))
