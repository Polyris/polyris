"""Every `ADR #N` / `adr-N-slug` reference in docs must resolve to a
known ADR — either an individual `docs/reference/adr-N-*.md` file or a
`### N. Title` heading in the monolithic `DESIGN_DECISIONS.md` archive.

Both are valid ADR homes: `DESIGN_DECISIONS.md` was the original archive
(#0-#100+), individual files came later. A reference to either counts.
"""
from __future__ import annotations

import re

import pytest

from tests.docs._helpers import (
    REPO_ROOT,
    format_findings,
    iter_docs,
    iter_lines,
    rel,
)


ADR_DIR = REPO_ROOT / "docs" / "reference"
DESIGN_DECISIONS = ADR_DIR / "DESIGN_DECISIONS.md"

# Matches `ADR 123`, `ADR #123`, `ADR-123`, `ADR-#123`, `adr-123-slug`.
_ADR_REF_RE = re.compile(r"(?:ADR[\s#-]+#?|adr-)(\d+)")
# Heading form in DESIGN_DECISIONS.md — earlier ADRs use `### 42. Title`,
# later ones (90+) dropped the dot: `### 90 Upstream...`. Accept both.
_DD_HEADING_RE = re.compile(r"^#{2,4}\s+(\d+)(?:\.|\s)", re.MULTILINE)


def _load_adr_numbers() -> set[int]:
    numbers: set[int] = set()
    for p in ADR_DIR.glob("adr-*.md"):
        m = re.match(r"adr-(\d+)-", p.name)
        if m:
            numbers.add(int(m.group(1)))
    if DESIGN_DECISIONS.exists():
        text = DESIGN_DECISIONS.read_text()
        for m in _DD_HEADING_RE.finditer(text):
            numbers.add(int(m.group(1)))
    return numbers


def test_adr_refs_resolve() -> None:
    known = _load_adr_numbers()
    findings: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int, int]] = set()
    for path in iter_docs():
        for lineno, line in iter_lines(path):
            for m in _ADR_REF_RE.finditer(line):
                num = int(m.group(1))
                if num in known:
                    continue
                key = (rel(path), lineno, num)
                if key in seen:
                    continue
                seen.add(key)
                findings.append((
                    rel(path),
                    lineno,
                    f"ADR #{num} — not in DESIGN_DECISIONS.md or docs/reference/adr-{num}-*.md",
                ))
    if findings:
        pytest.fail(format_findings("adr_refs", findings))
