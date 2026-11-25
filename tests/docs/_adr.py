"""Finds the design decisions (ADRs) and the citations of them.

One module, shared by the ADR tests in this folder: what an ADR is, where its
sections are, and what counts as a citation of one. Root `CLAUDE.md`
Principle #29 states the rule these tests enforce.

An ADR is cited as `ADR #N`, `ADR-N`, `adr-N-slug` or `ADR #64.1`; a section of
it as `ADR #N §k` (a list: `§1, §5`, `§§1, 5`, a range `§3–§5`), `ADR #N §Name`
or `ADR #N, *Name*`. A bare `§k` belongs to an ADR the same file cites.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Optional

from tests.docs._helpers import REPO_ROOT

ADR_DIR = REPO_ROOT / "docs" / "decisions"
INDEX = ADR_DIR / "README.md"
# The tag whose tree holds every retired ADR's text; its release is immutable.
RETIRED_TAG = "v1.3.2"

# Numbers that name two decisions, with the word a citation of each names right
# after the number ("ADR #65, view sync"). No other number may collide.
COLLIDING: dict[int, tuple[str, ...]] = {
    65: ("view sync", "API token"),
    94: ("runtime config", "error codes"),
    106: ("parameter parity", "date scope"),
    107: ("retry loop", "notifications"),
    108: ("TaskConfigKey", "runs feed"),
}
# How far after a colliding number its slug word may stand.
SLUG_WINDOW = 80

# Files whose text cites ADRs as examples of what the checks reject.
SELF_FILES = frozenset({
    "tests/docs/_adr.py",
    "tests/docs/test_adr_refs.py",
    "tests/docs/test_adr_sections.py",
})

TEXT_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".md", ".yaml", ".yml",
    ".json", ".sh", ".toml", ".cfg", ".ini", ".css",
})
TEXT_NAMES = frozenset({"Makefile", "Dockerfile"})

# `ADR #137`, `ADR-123`, `ADR 2`, `adr-137-tasks-as-rows`, `ADR #64.1`,
# `DESIGN_DECISIONS #72`. A word character before `adr-` (`test_adr-`) is not
# a citation.
CITE_RE = re.compile(
    r"(?:\bADR[ \t]*#[ \t]*|\bADR-#?|\bADR[ \t]+(?=\d)|(?<![\w-])adr-"
    r"|\bDESIGN_DECISIONS(?:\.md)?[ \t]+#)"
    r"(?P<num>\d+)(?P<sub>\.\d+(?!\.?\d))?(?:-(?P<slug>[a-z0-9][a-z0-9-]*))?"
)
_SEC = r"\d+(?:\.\d+)*"
_SEP = r"[ \t]*(?:,|and|–|-)[ \t]*"
# What follows a citation's number when it names a section.
SECTION_TAIL_RE = re.compile(
    r"(?:"
    rf"[ \t]*,?[ \t]*§§[ \t]*(?P<multi>{_SEC}(?:{_SEP}§?[ \t]*{_SEC})*)"
    rf"|[ \t]*,?[ \t]*§[ \t]*(?P<list>{_SEC}(?:{_SEP}§[ \t]*{_SEC})*)"
    r"|[ \t]*,?[ \t]*§[ \t]*(?P<name>[A-Z][A-Za-z]*(?:[ \t]+[A-Za-z]+)*)"
    r"|[ \t]*,?[ \t]+\*(?P<starred>[^*\s][^*]*?)\*(?!\*)"
    r")"
)
BARE_SECTION_RE = re.compile(
    rf"§§[ \t]*(?P<multi>{_SEC}(?:{_SEP}§?[ \t]*{_SEC})*)"
    rf"|§[ \t]*(?P<one>{_SEC})(?!\w)"  # `§7c` is another document's label
)
# A document named right before a bare `§`: `PLAN §2`, `SPIKE_X.md §9.5`,
# `Apache 2.0 §6`. That `§` is the document's, not an ADR's.
_DOC_BEFORE_RE = re.compile(r"(?:\.md`?|\b[A-Z][A-Z0-9_]{2,}|\b\d+\.\d+)[ \t]*$")
_SEC_ITEM_RE = re.compile(_SEC)

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?(?:[ \t]|$)")
_LIST_ITEM_RE = re.compile(r"^(\d+)\.[ \t]")
_TITLE_PREFIX_RE = re.compile(r"^(?:ADR[ \t]*[#-][ \t]*\d+[:.]?[ \t]*(?:—|-|:)?[ \t]*|\d+(?:\.\d+)?\.?[ \t]+)")
# A section replaced by a later decision keeps its heading and one line.
POINTER_RE = re.compile(r"^Superseded by ADR #(\d+)(?:[ \t]*§[ \t]*(\d+(?:\.\d+)*))?")


@dataclass
class Section:
    level: int
    title: str
    body: list[str]


@dataclass
class Adr:
    """One decision: its number, where its text is and its sections."""

    number: int
    slug: str
    path: Path
    lines: list[str]
    numbered: dict[str, Section] = field(default_factory=dict)
    sections: list[Section] = field(default_factory=list)
    list_items: set[str] = field(default_factory=set)

    @property
    def where(self) -> str:
        return self.path.relative_to(REPO_ROOT).as_posix()

    @property
    def title(self) -> str:
        m = _HEADING_RE.match(self.lines[0]) if self.lines else None
        return _TITLE_PREFIX_RE.sub("", m.group(2)) if m else ""

    def has_section(self, key: str) -> bool:
        if self.numbered:
            return key in self.numbered
        # An ADR whose decision is a numbered list cites its items as sections.
        return key in self.list_items

    def has_named(self, name: str, *, prefix: bool = False) -> bool:
        want = _plain(name).lower()
        for s in self.sections:
            have = _plain(s.title).lower()
            if have == want or (prefix and have.startswith(want)):
                return True
        return False

    def has_title(self, name: str) -> bool:
        return _plain(self.title).lower().startswith(_plain(name).lower())

    def pointers(self) -> Iterator[tuple[str, tuple[int, Optional[str]]]]:
        for s in self.sections:
            target = _pointer_target(s)
            if target:
                yield s.title, target

    def is_pointer(self, key: Optional[str]) -> bool:
        s = self.numbered.get(key) if key else None
        return bool(s and _pointer_target(s))


def _pointer_target(s: Section) -> Optional[tuple[int, Optional[str]]]:
    body = [line for line in s.body if line.strip()]
    if len(body) != 1:
        return None
    m = POINTER_RE.match(body[0])
    return (int(m.group(1)), m.group(2)) if m else None


def _plain(text: str) -> str:
    return re.sub(r"[`*_]", "", text).strip().rstrip(".:")


def _index_sections(adr: Adr) -> None:
    in_code = False
    open_sections: list[Section] = []
    for line in adr.lines[1:]:
        if line.lstrip().startswith("```"):
            in_code = not in_code
        m = None if in_code else _HEADING_RE.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2)
            section = Section(level, title, [])
            open_sections = [s for s in open_sections if s.level < level] + [section]
            adr.sections.append(section)
            n = _NUMBERED_HEADING_RE.match(title)
            if n:
                adr.numbered.setdefault(n.group(1), section)
            continue
        for s in open_sections:
            s.body.append(line)
        li = None if in_code else _LIST_ITEM_RE.match(line)
        if li:
            adr.list_items.add(li.group(1))


@lru_cache(maxsize=1)
def load_adrs() -> dict[int, list[Adr]]:
    """Every ADR in force, by number. A colliding number has several."""
    adrs: list[Adr] = []
    for p in sorted(ADR_DIR.glob("adr-[0-9]*.md")):
        m = re.match(r"adr-(\d+)-(.+)\.md$", p.name)
        if m:
            lines = p.read_text().splitlines()
            if lines[:1] == ["---"] and "---" in lines[1:]:
                lines = lines[lines.index("---", 1) + 1:]  # the front matter is no section
            adrs.append(Adr(int(m.group(1)), m.group(2), p, lines))
    found: dict[int, list[Adr]] = {}
    for adr in adrs:
        _index_sections(adr)
        found.setdefault(adr.number, []).append(adr)
    return found


@dataclass
class Retired:
    number: int
    title: str
    why: str
    command: str


_RETIRED_ROW_RE = re.compile(r"^\| (\d+) \| (.+?) \| (.+?) \| `(.+)` \|$")


@lru_cache(maxsize=1)
def load_retired() -> dict[int, Retired]:
    """The *Retired* rows of the index: numbers with no file, kept resolvable."""
    rows: dict[int, Retired] = {}
    inside = False
    for line in INDEX.read_text().splitlines():
        if line.startswith("## "):
            inside = line.strip() == "## Retired"
            continue
        m = _RETIRED_ROW_RE.match(line) if inside else None
        if m:
            rows[int(m.group(1))] = Retired(int(m.group(1)), m.group(2), m.group(3), m.group(4).replace("\\|", "|"))
    return rows


@lru_cache(maxsize=1)
def tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True,
    ).stdout.decode()
    files: list[Path] = []
    for name in out.split("\0"):
        if not name or name in SELF_FILES:
            continue
        p = REPO_ROOT / name
        if (p.suffix in TEXT_SUFFIXES or p.name in TEXT_NAMES) and p.name != "package-lock.json" and p.is_file():
            files.append(p)
    return files


def scanned_lines(path: Path) -> list[tuple[int, str]]:
    """The lines of a file the citation checks read.

    `CHANGELOG.md` is history once released: only `## Unreleased` is read.
    """
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    numbered = list(enumerate(lines, start=1))
    if path == REPO_ROOT / "CHANGELOG.md":
        kept: list[tuple[int, str]] = []
        inside = False
        for n, line in numbered:
            if line.startswith("## "):
                inside = line.strip() == "## Unreleased"
                continue
            if inside:
                kept.append((n, line))
        return kept
    return numbered


@dataclass
class Citation:
    path: Path
    lineno: int
    number: int
    sub: Optional[str]
    slug: Optional[str]
    sections: list[str]
    name: Optional[str]
    starred: Optional[str]
    text: str
    span: tuple[int, int]
    form: str


def citations_in(path: Path, lineno: int, line: str) -> list[Citation]:
    found: list[Citation] = []
    for m in CITE_RE.finditer(line):
        tail = None if m.group("slug") else SECTION_TAIL_RE.match(line, m.end())
        sections: list[str] = []
        name = starred = None
        end = m.end()
        if tail:
            end = tail.end()
            items = tail.group("multi") or tail.group("list")
            if items:
                # A range `§3–§5` names its ends; the ends are what must exist.
                sections = _SEC_ITEM_RE.findall(items)
            name, starred = tail.group("name"), tail.group("starred")
        form = "DESIGN_DECISIONS" if m.group(0).startswith("DESIGN_DECISIONS") else "ADR"
        found.append(Citation(
            path, lineno, int(m.group("num")), m.group("sub"), m.group("slug"),
            sections, name, starred, line, (m.start(), end), form,
        ))
    return found


def is_own_title(c: Citation) -> bool:
    """The heading of an ADR's own file names its number without a slug word."""
    return c.text.startswith("#") and bool(re.match(rf"adr-{c.number}-", c.path.name))


def own_numbered_headings(lines: list[tuple[int, str]]) -> set[str]:
    keys: set[str] = set()
    for _, line in lines:
        m = _HEADING_RE.match(line)
        if m:
            n = _NUMBERED_HEADING_RE.match(m.group(2).lstrip("§ "))
            if n:
                keys.add(n.group(1))
    return keys


def bare_sections(line: str, taken: list[tuple[int, int]], previous: str = "") -> list[str]:
    """The `§k` keys of a line that no citation took and no document name owns."""
    keys: list[str] = []
    for m in BARE_SECTION_RE.finditer(line):
        if any(a <= m.start() < b for a, b in taken):
            continue
        before = line[:m.start()]
        if not before.strip():
            before = previous
        if _DOC_BEFORE_RE.search(before):
            continue
        keys.extend(_SEC_ITEM_RE.findall(m.group(0)))
    return keys
