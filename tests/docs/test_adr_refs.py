"""Every ADR number cited anywhere in the repository names a decision.

Code, tests, CLAUDE.md files, skills and docs cite ADRs as rules (root
`CLAUDE.md` Principle #29). The scan reads every tracked text file; in
`CHANGELOG.md` only `## Unreleased`, since released sections are history.
"""
from __future__ import annotations

import pytest

from tests.docs import _adr
from tests.docs._helpers import format_findings, rel


def _all_citations() -> list[_adr.Citation]:
    found: list[_adr.Citation] = []
    for path in _adr.tracked_text_files():
        for lineno, line in _adr.scanned_lines(path):
            found.extend(_adr.citations_in(path, lineno, line))
    return found


def test_every_cited_adr_number_resolves() -> None:
    known = set(_adr.load_adrs()) | set(_adr.load_retired())
    findings: list[tuple[str, int, str]] = []
    for c in _all_citations():
        if c.form != "ADR":
            findings.append((rel(c.path), c.lineno, f"cite it as `ADR #{c.number}`, not by the old file name"))
        elif c.number not in known:
            findings.append((rel(c.path), c.lineno, f"ADR #{c.number} names no decision"))
    if findings:
        pytest.fail(format_findings("adr_refs", findings))


# What an agent reads as standing instructions cites only decisions in force.
def _is_instruction(path: str) -> bool:
    return (
        path.endswith("CLAUDE.md")
        or path.startswith(".claude/skills/")
        or path == ".github/workflows/claude-code-review.yml"
    )


def test_instructions_cite_no_retired_adr() -> None:
    retired = _adr.load_retired()
    findings: list[tuple[str, int, str]] = []
    for c in _all_citations():
        if c.number in retired and _is_instruction(rel(c.path)):
            findings.append((rel(c.path), c.lineno, f"ADR #{c.number} is retired: {retired[c.number].why}"))
    if findings:
        pytest.fail(format_findings("adr_retired_in_instructions", findings))


def test_only_the_known_numbers_name_two_decisions() -> None:
    colliding = {n for n, adrs in _adr.load_adrs().items() if len(adrs) > 1}
    allowed = set(_adr.COLLIDING)
    assert colliding <= allowed, (
        f"ADR numbers {sorted(colliding - allowed)} name two decisions; "
        "a new ADR takes the highest number + 1"
    )


def test_a_colliding_number_is_cited_with_its_slug_word() -> None:
    findings: list[tuple[str, int, str]] = []
    for c in _all_citations():
        words = _adr.COLLIDING.get(c.number)
        if not words or c.slug or _adr.is_own_title(c):
            continue
        after = c.text[c.span[0]:c.span[0] + _adr.SLUG_WINDOW].lower().replace("`", "")
        named = [w for w in words if w.lower() in after]
        if len(named) != 1:
            findings.append((
                rel(c.path), c.lineno,
                f"ADR #{c.number} names two decisions: write `ADR #{c.number}, <{' | '.join(words)}>`",
            ))
    if findings:
        pytest.fail(format_findings("adr_colliding", findings))
