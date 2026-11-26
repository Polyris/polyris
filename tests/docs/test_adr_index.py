"""The ADR index, `docs/decisions/README.md`, agrees with the ADR files.

Its table of ADRs in force is generated from each file's front matter by
`scripts/adr_index.py`; root `CLAUDE.md` Principle #29.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from types import ModuleType

import pytest

from tests.docs import _adr
from tests.docs._helpers import REPO_ROOT



def _index_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("adr_index", REPO_ROOT / "scripts" / "adr_index.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_adr_file_opens_with_its_front_matter_and_title() -> None:
    index = _index_module()
    problems: list[str] = []
    for path in index.adr_files():
        try:
            meta, body = index.read_front_matter(path)
        except ValueError as e:
            problems.append(str(e))
            continue
        if meta.get("area") not in index.AREAS:
            problems.append(f"{path.name}: area {meta.get('area')!r} is not one of {sorted(index.AREAS)}")
        if not meta.get("accepted"):
            problems.append(f"{path.name}: no `accepted`")
        number = path.name.split("-")[1]
        if not body or not body[0].startswith(f"# ADR #{number} — "):
            problems.append(f"{path.name}: the H1 is not `# ADR #{number} — <the decision>`")
    if problems:
        pytest.fail("\n".join(problems))


def test_the_index_table_is_generated_from_the_files() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/adr_index.py", "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_retired_number_has_no_file_and_its_row_finds_its_old_text() -> None:
    adrs = _adr.load_adrs()
    retired = _adr.load_retired()
    problems: list[str] = []
    assert retired, "no row under *Retired* parsed: `| N | title | why | `<tag>:<path>`[, lines a–b] |`"
    for n, row in retired.items():
        if n in adrs:
            problems.append(f"#{n}: retired, yet {adrs[n][0].where} exists")
        if "ADR #" not in row.why and not row.why.startswith(("Change record", "Its subject is gone")):
            problems.append(f"#{n}: the row names no successor and no reason")
        if not _adr.RELEASE_TAG_RE.match(row.tag):
            problems.append(f"#{n}: {row.tag!r} is not a release tag (vX.Y.Z)")
            continue
        shown = subprocess.run(["git", "show", f"{row.tag}:{row.path}"], cwd=REPO_ROOT, capture_output=True, text=True)
        if shown.returncode != 0:
            problems.append(f"#{n}: {shown.stderr.strip()} (in a shallow clone: git fetch origin tag {row.tag})")
            continue
        lines = shown.stdout.splitlines()
        first = lines[row.lines[0] - 1] if row.lines else lines[0]
        if not re.match(rf"^#+ (?:ADR #)?{n}\b", first):
            problems.append(f"#{n}: the first line of its text is {first!r}, not ADR #{n}'s heading")
    if problems:
        pytest.fail("\n".join(problems))
