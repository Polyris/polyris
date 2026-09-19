"""Every `polyris-<cmd> --<flag>` in a shell-invocation position must
exist as an `add_argument("--<flag>")` somewhere in `polyris/*.py`.

Handles backslash-continuation lines (`polyris-register --name x \\\\\\n
    --region y`) — flags on continuation lines are scanned together with
the anchor.

Rejects false positives from:
- prose mentions (`the polyris-deploy command…`) — anchor requires
  line-start / prompt / backtick prefix;
- `polyris` as a positional AWS CLI argument (`--stack-name polyris`) —
  same anchor filter;
- flags after `|`, `>`, `#`, backtick — different command / comment /
  end of inline code.
"""
from __future__ import annotations

import re
import tomllib

import pytest

from tests.docs._helpers import (
    REPO_ROOT,
    format_findings,
    iter_docs,
    iter_lines,
    rel,
)


POLYRIS_DIR = REPO_ROOT / "polyris"
PYPROJECT = REPO_ROOT / "pyproject.toml"

_CMD_ANCHOR_TEMPLATE = r"(?:^\s*|[$>]\s+|`)({cmd})(?!\S)"
_FLAG_TOKEN_RE = re.compile(r"(?<![-\w])(--?[a-zA-Z][-a-zA-Z0-9]*)")
_ADD_ARGUMENT_RE = re.compile(
    r"add_argument\(\s*"
    r"((?:[\"'][-a-zA-Z0-9_]+[\"']\s*,\s*)*[\"'][-a-zA-Z0-9_]+[\"'])"
)


def _load_code_flags() -> set[str]:
    flags: set[str] = set()
    for py in POLYRIS_DIR.rglob("*.py"):
        try:
            text = py.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for m in _ADD_ARGUMENT_RE.finditer(text):
            for tok in re.findall(r"[\"']([-a-zA-Z0-9_]+)[\"']", m.group(1)):
                if tok.startswith("-"):
                    flags.add(tok)
    return flags


def _load_entry_points() -> set[str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return set(data.get("project", {}).get("scripts", {}).keys())


def _iter_logical_lines(path):
    """Yield (anchor_lineno, joined_text, offset_map).

    Backslash-continued physical lines are joined into one logical line.
    `offset_map[i]` gives the physical lineno of char i in `joined_text`,
    so findings can be reported on the actual line where the flag lives.
    """
    physical = list(iter_lines(path))
    i = 0
    while i < len(physical):
        anchor_lineno, first = physical[i]
        joined = first.rstrip("\n")
        offset_map = [anchor_lineno] * len(joined)
        while joined.endswith("\\") and i + 1 < len(physical):
            joined = joined[:-1] + " "
            offset_map = offset_map[:-1] + [anchor_lineno]
            i += 1
            next_lineno, next_line = physical[i]
            next_text = next_line.rstrip("\n")
            joined += next_text
            offset_map.extend([next_lineno] * len(next_text))
        yield anchor_lineno, joined, offset_map
        i += 1


def test_cli_flags_referenced_in_docs_exist() -> None:
    known_flags = _load_code_flags()
    entry_points = _load_entry_points()
    findings: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, str, str]] = set()

    for cmd in sorted(entry_points):
        pattern = re.compile(_CMD_ANCHOR_TEMPLATE.format(cmd=re.escape(cmd)))
        for path in iter_docs():
            for _anchor, joined, offsets in _iter_logical_lines(path):
                for cmd_match in pattern.finditer(joined):
                    after = joined[cmd_match.end():]
                    after_offsets = offsets[cmd_match.end():]
                    for sep in ("|", ">", "#", "`", "<br"):
                        idx = after.find(sep)
                        if idx >= 0:
                            after = after[:idx]
                            after_offsets = after_offsets[:idx]
                    for m in _FLAG_TOKEN_RE.finditer(after):
                        flag = m.group(1)
                        if flag in ("-h", "--help"):
                            continue
                        if flag in known_flags:
                            continue
                        lineno = (
                            after_offsets[m.start()]
                            if m.start() < len(after_offsets)
                            else _anchor
                        )
                        key = (rel(path), lineno, flag, cmd)
                        if key in seen:
                            continue
                        seen.add(key)
                        findings.append((
                            rel(path),
                            lineno,
                            f"flag {flag!r} on {cmd!r} — no add_argument({flag!r}) in polyris/*.py",
                        ))
    if findings:
        pytest.fail(format_findings("cli_flags", findings))
