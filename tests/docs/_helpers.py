"""Shared loaders for docs audit tests.

Each `test_*.py` in this directory implements one drift check between docs
and code. They all share the same doc-discovery + suppression rules,
extracted here to avoid drift between them.

Suppression:
- Whole file — matches `DEFAULT_SKIP_GLOBS` (historical ADR archives,
  spikes, changelog, meta-docs).
- Single line — append `<!-- audit-docs: skip-line -->` at end of line.
"""
from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"

# Historical / frozen docs — allowed to reference removed symbols, dead
# paths, obsolete flags. `adr-[0-9]*.md` matches only numbered ADR files;
# `adr-index.md` is the live index (Principle #29) and must stay audited.
DEFAULT_SKIP_GLOBS = (
    "docs/reference/DESIGN_DECISIONS.md",
    "docs/reference/adr-[0-9]*.md",
    "docs/reference/SPIKE_*.md",
    "docs/reference/COMPLETENESS_REPORT_*.md",
    "docs/CLAUDE.md",
    "CHANGELOG.md",
)

SUPPRESS_MARKER = "<!-- audit-docs: skip-line -->"


def iter_docs() -> list[Path]:
    """Every `docs/**/*.md` plus root `README.md`, minus skip globs."""
    files: list[Path] = []
    for p in DOCS_DIR.rglob("*.md"):
        rel = p.relative_to(REPO_ROOT).as_posix()
        if any(fnmatch.fnmatch(rel, g) for g in DEFAULT_SKIP_GLOBS):
            continue
        files.append(p)
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    return sorted(files)


def iter_lines(path: Path) -> Iterable[tuple[int, str]]:
    """Yield (lineno, line) — 1-indexed, empty on read error."""
    try:
        with path.open() as fh:
            for i, line in enumerate(fh, start=1):
                yield i, line
    except (OSError, UnicodeDecodeError):
        return


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def has_suppress(line: str) -> bool:
    return SUPPRESS_MARKER in line


def format_findings(category: str, findings: list[tuple[str, int, str]]) -> str:
    """Format `[(file, line, message), ...]` into a pytest failure body."""
    lines = [f"docs audit [{category}]: {len(findings)} finding(s)"]
    for file, lineno, message in sorted(findings):
        lines.append(f"  {file}:{lineno}  {message}")
    return "\n".join(lines)
