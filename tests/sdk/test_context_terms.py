"""
Guard for the context documents (CONTEXT.md, STATE.md).

A glossary that drifts is worse than no glossary: agents and new contributors
trust it, and a stale entry sends them confidently wrong. CLAUDE.md demonstrated
exactly this — five of its claims were false before a guard existed.

So every term in CONTEXT.md must still appear somewhere in the code, and every
path STATE.md references must still exist. Neither check proves the *definition*
is right; they prove the vocabulary has not rotted out from under the file.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ROOT / ".claude" / "CONTEXT.md"
STATE = ROOT / "STATE.md"

# Terms that are deliberately vocabulary-only: they name a discipline or an
# absence, so there is no identifier to find. Each needs a reason.
NOT_IN_CODE = {
    "catchup": "documented as NOT a polyris concept — its absence is the point",
    "re-run": "prose term for a user action, not an identifier",
    "blast radius": "review discipline (CLAUDE.md #23), not code",
    "mutation test": "review discipline, not code",
    "gate": "prose term; the gates themselves are Makefile targets",
    "guard test": "prose term for this category of test",
    "drift": "prose term; the checkers are named check_*",
    "measured core": "prose term for the coverage omit boundary",
    "paid surface": "spelled paidSurface in code; covered by PaidSurface below",
    "OSS build": "prose term for a build with src/ee absent",
    "asset event": "spelled asset_event / AssetEvents in code",
    "free": "tier name, not an identifier",
    "Team / Enterprise": "tier names, checked via entitlements elsewhere",
    "CE": "repo shorthand",
    "EE": "repo shorthand",
}


def _terms():
    """Pull the bolded first column out of every markdown table row."""
    if not CONTEXT.exists():
        pytest.skip("CONTEXT.md not present")
    out = []
    for line in CONTEXT.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*\*\*(.+?)\*\*\s*\|", line)
        if m:
            out.append(m.group(1).strip())
    return out


def _haystack():
    parts = []
    for sub in ("polyris", "sam", "ui/src", "tests"):
        for p in (ROOT / sub).rglob("*"):
            if not p.is_file() or p.suffix not in {".py", ".ts", ".tsx", ".json", ".yaml", ".yml"}:
                continue
            if "node_modules" in str(p) or "__pycache__" in str(p):
                continue
            try:
                parts.append(p.read_text(encoding="utf-8", errors="ignore"))
            except OSError:
                pass
    return "\n".join(parts)


def test_context_md_has_terms():
    terms = _terms()
    assert len(terms) > 20, (
        f"CONTEXT.md parsed to only {len(terms)} terms — the table format probably "
        f"changed and this guard stopped guarding anything."
    )


def test_every_glossary_term_still_exists_in_the_code():
    """A term nobody uses any more is drift. Either the code dropped it (remove
    the entry) or it was renamed (update the entry)."""
    hay = _haystack()
    missing = []
    for term in _terms():
        if term in NOT_IN_CODE:
            continue
        # snake_case, camelCase and plain forms all count as present
        candidates = {
            term,
            term.replace(" ", "_"),
            term.replace("_", ""),
            term.replace(" ", ""),
        }
        if not any(c and c in hay for c in candidates):
            missing.append(term)
    assert not missing, (
        "CONTEXT.md defines terms that no longer appear in the code:\n  "
        + "\n  ".join(missing)
        + "\nRemove the entry, or add it to NOT_IN_CODE with a reason."
    )


def test_exemptions_all_carry_a_reason():
    for term, reason in NOT_IN_CODE.items():
        assert reason and len(reason) > 10, (
            f"NOT_IN_CODE[{term!r}] needs a written reason, not a placeholder."
        )


def test_state_md_references_only_paths_that_exist():
    """STATE.md points at work in flight. A path that has moved makes it a map to
    nowhere, which is how a status file quietly becomes fiction."""
    if not STATE.exists():
        pytest.skip("STATE.md not present")
    text = STATE.read_text(encoding="utf-8")
    # backtick-quoted things that look like repo paths
    paths = re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|ts|tsx|md|sh|json|yaml|yml))`", text)
    missing = [p for p in set(paths) if not (ROOT / p).exists()]
    assert not missing, (
        "STATE.md references paths that do not exist:\n  " + "\n  ".join(sorted(missing))
    )
