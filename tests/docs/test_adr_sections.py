"""Every cited section of an ADR exists in that ADR.

`ADR #137 §7`, `ADR #130 §1, §9`, `ADR #137, *Migration and upgrade*` and
`ADR #50 §Backward compatibility` resolve to a heading of that ADR (or to an
item of its numbered decision list, when it has no numbered headings). A bare
`§k` resolves in an ADR the same file cites, unless a document name stands
right before it (`PLAN §2`, `SPIKE_X.md §9.5`) or the file has its own `k.`
heading. Root `CLAUDE.md` Principle #29.
"""
from __future__ import annotations

import pytest

from tests.docs import _adr
from tests.docs._helpers import format_findings, rel


def _check(c: _adr.Citation, adrs: list[_adr.Adr]) -> list[str]:
    missing: list[str] = []
    for key in c.sections:
        if not any(a.has_section(key) for a in adrs):
            missing.append(f"ADR #{c.number} §{key}")
    if c.sub and not any(a.has_section(f"{c.number}{c.sub}") for a in adrs):
        missing.append(f"ADR #{c.number}{c.sub}")
    if c.name and not any(a.has_named(c.name, prefix=True) for a in adrs):
        missing.append(f"ADR #{c.number} §{c.name}")
    if c.starred and not any(a.has_named(c.starred) or a.has_title(c.starred) for a in adrs):
        missing.append(f"ADR #{c.number}, *{c.starred}*")
    return missing


def test_every_cited_section_exists() -> None:
    known = _adr.load_adrs()
    findings: list[tuple[str, int, str]] = []
    for path in _adr.tracked_text_files():
        for lineno, line in _adr.scanned_lines(path):
            for c in _adr.citations_in(path, lineno, line):
                if c.number in known:
                    for what in _check(c, known[c.number]):
                        findings.append((rel(path), lineno, f"{what}: no such section"))
    if findings:
        pytest.fail(format_findings("adr_sections", findings))


def test_a_bare_section_resolves_in_an_adr_the_file_cites() -> None:
    known = _adr.load_adrs()
    findings: list[tuple[str, int, str]] = []
    for path in _adr.tracked_text_files():
        lines = _adr.scanned_lines(path)
        citations = [c for n, line in lines for c in _adr.citations_in(path, n, line)]
        cited = [a for num in {c.number for c in citations} for a in known.get(num, [])]
        if not cited:
            continue  # a `§` in a file that cites no ADR is about something else
        own = _adr.own_numbered_headings(lines)
        taken: dict[int, list[tuple[int, int]]] = {}
        for c in citations:
            taken.setdefault(c.lineno, []).append(c.span)
        previous = ""
        for lineno, line in lines:
            for key in _adr.bare_sections(line, taken.get(lineno, []), previous):
                if key in own or any(a.has_section(key) for a in cited):
                    continue
                findings.append((rel(path), lineno, f"§{key} is in no ADR this file cites"))
            previous = line
    if findings:
        pytest.fail(format_findings("adr_bare_sections", findings))


def test_a_pointer_leads_to_a_live_section() -> None:
    known = _adr.load_adrs()
    findings: list[tuple[str, int, str]] = []
    for adrs in known.values():
        for adr in adrs:
            for heading, target in adr.pointers():
                num, key = target
                live = [a for a in known.get(num, []) if key is None or a.has_section(key)]
                if not live:
                    findings.append((adr.where, 0, f"{heading} points at ADR #{num} §{key}, which does not exist"))
                elif any(a.is_pointer(key) for a in live):
                    findings.append((adr.where, 0, f"{heading} points at another pointer (ADR #{num} §{key})"))
    if findings:
        pytest.fail(format_findings("adr_pointers", findings))
