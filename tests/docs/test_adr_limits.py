"""Every ADR fits one read, and an ADR of the paid overlay never lives here.

The limits keep an ADR to the rule in force (`docs/CLAUDE.md`, *Design
decisions*): a file over 20 KB is split by sub-decision first. The files over
it are listed below with a cap at their size; a cap goes down with its file and
goes up only by the maintainer's decision, written next to it.

No ADR numbered from 1001, the overlay's numbers (root `CLAUDE.md`
Principle #24), is stored or cited here. The seam ADRs, which describe the
free/paid boundary itself, are named once, in `scripts/check-oss-purity.sh`;
`.config/vale.ini` must name the same ones.
"""
from __future__ import annotations

import re

import pytest

from tests.docs import _adr
from tests.docs._helpers import REPO_ROOT, format_findings, rel

FILE_LIMIT = 20_000
LINE_LIMIT = 1_000
HEADER_FIELD_LIMIT = 300
TITLE_LIMIT = 120
# A cap that is this far above its file's size must come down to it.
CAP_SLACK = 1_024

# Files over FILE_LIMIT, each capped at its size when the limit came in (1.3.2).
OVER_THE_LIMIT = {
    # hub of the engine; compressed from 179 KB, then raised by the review of the branch to
    # keep the probed AWS behaviour that code and tests cite (*Probed AWS behaviour*)
    "adr-137-tasks-as-rows.md": 121_712,
    # hub; compressed from 67 KB, then raised by the same review for §6.1's DependsOn rule
    "adr-130-coordination-reconciler.md": 34_900,
    "adr-51-backfill-unification.md": 24_800,  # era spec, not yet split
    "adr-138-trigger-rules-after-run-end-change.md": 23_400,  # its two truth tables are the decision
}

# The first number an ADR of the paid overlay takes (root CLAUDE.md Principle #24).
FIRST_OVERLAY_NUMBER = 1001


def _adr_files() -> list:
    return sorted(_adr.ADR_DIR.glob("adr-[0-9]*.md"))


def test_every_adr_fits_its_limits() -> None:
    findings: list[tuple[str, int, str]] = []
    for path in _adr_files():
        size = len(path.read_bytes())
        cap = OVER_THE_LIMIT.get(path.name, FILE_LIMIT)
        if size > cap:
            findings.append((rel(path), 0, f"{size:,} bytes, over its limit of {cap:,}: split it by sub-decision, "
                                           "or cut history (git and the CHANGELOG keep it)"))
        elif path.name in OVER_THE_LIMIT and size < cap - CAP_SLACK:
            findings.append((rel(path), 0, f"{size:,} bytes: lower its cap in OVER_THE_LIMIT to its size"))
        lines = path.read_text().splitlines()
        in_header = bool(lines) and lines[0] == "---"
        for n, line in enumerate(lines, start=1):
            if len(line) > LINE_LIMIT:
                findings.append((rel(path), n, f"{len(line):,} characters on one line, over {LINE_LIMIT:,}: "
                                               "the Read tool cuts long lines"))
            if in_header and n > 1:
                if line == "---":
                    in_header = False
                elif len(line) > HEADER_FIELD_LIMIT:
                    findings.append((rel(path), n, f"a header field over {HEADER_FIELD_LIMIT} characters"))
            m = re.match(r"^# ADR #\d+ — (.*)$", line)
            if m and len(m.group(1)) > TITLE_LIMIT:
                findings.append((rel(path), n, f"a title over {TITLE_LIMIT} characters: the title states the decision"))
    if findings:
        pytest.fail(format_findings("adr_limits", findings))


def test_the_allowlist_names_only_files_over_the_limit() -> None:
    names = {p.name for p in _adr_files()}
    assert set(OVER_THE_LIMIT) <= names, f"no such ADR file: {sorted(set(OVER_THE_LIMIT) - names)}"


def _is_overlay(number: int) -> bool:
    return number >= FIRST_OVERLAY_NUMBER


def test_no_adr_of_the_paid_overlay_lives_here() -> None:
    findings: list[tuple[str, int, str]] = []
    for path in _adr.ADR_DIR.glob("adr-*.md"):
        m = re.match(r"adr-(\d+)-", path.name)
        if m and _is_overlay(int(m.group(1))):
            findings.append((rel(path), 0, "an ADR of the paid overlay: it lives in polyris-ee/docs/decisions/"))
    for path in _adr.tracked_text_files():
        for lineno, line in _adr.scanned_lines(path):
            for c in _adr.citations_in(path, lineno, line):
                if _is_overlay(c.number):
                    findings.append((rel(path), lineno, f"ADR #{c.number} governs only the paid overlay: "
                                                        "this repository neither stores, indexes nor cites it"))
    if findings:
        pytest.fail(format_findings("adr_overlay", findings))


def test_vale_exempts_the_seam_adrs_the_purity_check_names() -> None:
    script = (REPO_ROOT / "scripts" / "check-oss-purity.sh").read_text()
    vale = (REPO_ROOT / ".config" / "vale.ini").read_text()
    m = re.search(r"^SEAM_ADRS=\(([\d ]+)\)", script, re.M)
    v = re.search(r"^\[docs/decisions/adr-\{([\d,]+)\}-\*\.md\]\nPolyrisDocs\.PaidFeatureLeaks = NO", vale, re.M)
    assert m and v, "SEAM_ADRS in check-oss-purity.sh, or its section in vale.ini, is missing"
    assert sorted(m.group(1).split()) == sorted(v.group(1).split(",")), (
        "vale.ini exempts other seam ADRs than scripts/check-oss-purity.sh names"
    )
