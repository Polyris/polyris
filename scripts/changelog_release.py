#!/usr/bin/env python3
"""Cut the CHANGELOG for a release — called by scripts/release.sh.

    python3 scripts/changelog_release.py check vX.Y.Z           # before the gates: notes exist
    python3 scripts/changelog_release.py cut vX.Y.Z YYYY-MM-DD  # after them: name the notes

``check`` fails unless CHANGELOG.md opens with ``## Unreleased`` and that section holds
something. ``cut`` turns the heading into ``## vX.Y.Z - YYYY-MM-DD`` and opens a new, empty
``## Unreleased`` above it. Both accept a changelog already cut for that version, so a
release re-run after a failed push goes on. Line endings are kept. Releases from 1.0.2 to
1.2.2 went out with their notes still under ``## Unreleased``: nothing renamed the heading.
"""
from __future__ import annotations

import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"
UNRELEASED = "## Unreleased\n"


def unreleased_notes(text: str) -> str:
    """The body of the ``## Unreleased`` section the file opens with; raises when it does not."""
    if not text.startswith(UNRELEASED):
        raise ValueError("CHANGELOG.md must open with '## Unreleased', the notes of the next release")
    return text[len(UNRELEASED):].split("\n## ", 1)[0]


def already_cut(text: str, version: str) -> bool:
    """An empty ``## Unreleased`` right above ``## <version> - …``: this release was cut before."""
    rest = text[len(UNRELEASED):] if text.startswith(UNRELEASED) else ""
    return not unreleased_notes(text).strip() and rest.lstrip("\n").startswith(f"## {version} - ")


def check(text: str, version: str) -> None:
    if not unreleased_notes(text).strip() and not already_cut(text, version):
        raise ValueError("CHANGELOG.md: '## Unreleased' is empty — write this release's notes first")


def cut(text: str, version: str, date: str) -> str:
    """The changelog with the Unreleased notes under ``## <version> - <date>``."""
    check(text, version)
    if already_cut(text, version):
        return text
    return f"{UNRELEASED}\n## {version} - {date}\n" + text[len(UNRELEASED):]


def _read(path: Path) -> "tuple[str, str]":
    raw = path.read_bytes().decode("utf-8")
    newline = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), newline


def main(argv: list[str]) -> int:
    try:
        text, newline = _read(CHANGELOG)
        if argv[:1] == ["check"] and len(argv) == 2:
            check(text, argv[1])
        elif argv[:1] == ["cut"] and len(argv) == 3:
            CHANGELOG.write_bytes(cut(text, argv[1], argv[2]).replace("\n", newline).encode("utf-8"))
        else:
            print("usage: changelog_release.py check vX.Y.Z | cut vX.Y.Z YYYY-MM-DD", file=sys.stderr)
            return 2
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
