#!/usr/bin/env python3
"""Cut the CHANGELOG for a release — called by scripts/release.sh.

    python3 scripts/changelog_release.py check            # before the gates: notes exist
    python3 scripts/changelog_release.py cut v1.2.3 DATE  # after them: name the notes

``check`` fails unless CHANGELOG.md opens with ``## Unreleased`` and that section holds
something. ``cut`` turns the heading into ``## v1.2.3 - DATE`` and opens a new, empty
``## Unreleased`` above it. Two releases in a row (1.2.1, 1.2.2) went out with their notes
still under ``## Unreleased``, because nothing renamed the heading.
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


def check(text: str) -> None:
    if not unreleased_notes(text).strip():
        raise ValueError("CHANGELOG.md: '## Unreleased' is empty — write this release's notes first")


def cut(text: str, version: str, date: str) -> str:
    """The changelog with the Unreleased notes under ``## <version> - <date>``."""
    check(text)
    return f"{UNRELEASED}\n## {version} - {date}\n" + text[len(UNRELEASED):]


def main(argv: list[str]) -> int:
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
        if argv[:1] == ["check"] and len(argv) == 1:
            check(text)
        elif argv[:1] == ["cut"] and len(argv) == 3:
            CHANGELOG.write_text(cut(text, argv[1], argv[2]), encoding="utf-8")
        else:
            print("usage: changelog_release.py check | cut vX.Y.Z YYYY-MM-DD", file=sys.stderr)
            return 2
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
