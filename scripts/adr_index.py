#!/usr/bin/env python3
"""Generate the table of ADRs in force in `docs/decisions/README.md`.

The table between the two markers is built from each ADR file's front matter
(`area`, `accepted`) and its H1; the rest of the README, the *Retired* table
included, is kept by hand. `--check` fails when the table is out of date
(`tests/docs/test_adr_index.py` runs it).

    python scripts/adr_index.py          # rewrite the table
    python scripts/adr_index.py --check  # exit 1 if it is out of date
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DECISIONS = Path(__file__).resolve().parent.parent / "docs" / "decisions"
README = DECISIONS / "README.md"
BEGIN = "<!-- BEGIN GENERATED: python scripts/adr_index.py -->"
END = "<!-- END GENERATED -->"
AREAS = {
    "engine": "Engine",
    "sdk": "SDK",
    "deploy": "Deploy",
    "console-api": "Console API",
    "ui": "UI",
    "open-core": "Open core",
    "docs": "Docs",
    "process": "Process",
}
FIELDS = ("area", "accepted", "revised", "supersedes", "amends")
_FILE_RE = re.compile(r"^adr-(\d+)-[a-z0-9-]+\.md$")
_H1_RE = re.compile(r"^# ADR #(\d+) — (.+)$")


def read_front_matter(path: Path) -> tuple[dict[str, str], list[str]]:
    """The front matter of an ADR file and the lines after it (from the H1 on)."""
    lines = path.read_text().splitlines()
    if not lines or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path.name}: no front matter")
    end = lines.index("---", 1)
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep or key not in FIELDS or key in meta:
            raise ValueError(f"{path.name}: front matter line {line!r}")
        meta[key] = value.strip().strip('"')
    return meta, lines[end + 1:]


def adr_files() -> list[Path]:
    files = [p for p in DECISIONS.glob("adr-*.md") if _FILE_RE.match(p.name)]
    return sorted(files, key=lambda p: (int(_FILE_RE.match(p.name).group(1)), p.name))  # type: ignore[union-attr]


def table() -> str:
    rows: dict[str, list[str]] = {area: [] for area in AREAS}
    for path in adr_files():
        meta, body = read_front_matter(path)
        h1 = _H1_RE.match(body[0]) if body else None
        if not h1 or int(h1.group(1)) != int(_FILE_RE.match(path.name).group(1)):  # type: ignore[union-attr]
            raise ValueError(f"{path.name}: the first line after the front matter is not `# ADR #N — title`")
        if meta.get("area") not in AREAS or "accepted" not in meta:
            raise ValueError(f"{path.name}: `area` (one of {', '.join(AREAS)}) and `accepted` are required")
        title = h1.group(2).replace("|", "\\|")
        rows[meta["area"]].append(f"| [#{h1.group(1)}]({path.name}) | {title} | {meta['accepted']} |")
    out = [BEGIN]
    for area, label in AREAS.items():
        if rows[area]:
            out += ["", f"## {label}", "", "| ADR | Decision | Accepted |", "|---|---|---|", *rows[area]]
    out += ["", END]
    return "\n".join(out)


def render(readme: str) -> str:
    start, end = readme.index(BEGIN), readme.index(END) + len(END)
    return readme[:start] + table() + readme[end:]


def main(argv: list[str]) -> int:
    current = README.read_text()
    wanted = render(current)
    if "--check" in argv:
        if current != wanted:
            print("docs/decisions/README.md is out of date: run `python scripts/adr_index.py`")
            return 1
        return 0
    README.write_text(wanted)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
