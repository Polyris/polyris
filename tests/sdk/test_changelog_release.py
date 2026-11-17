"""scripts/changelog_release.py — release.sh names the Unreleased notes after the version.

1.2.1 and 1.2.2 went out with their notes still under ``## Unreleased``: nothing renamed it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("changelog_release", REPO_ROOT / "scripts" / "changelog_release.py")
assert _spec and _spec.loader
changelog = importlib.util.module_from_spec(_spec)
sys.modules["changelog_release"] = changelog
_spec.loader.exec_module(changelog)

NOTES = "## Unreleased\n\n### Fixed — a thing\n\n- It works.\n\n## v1.2.2 - 2026-11-17\n\n- Older.\n"


def test_the_repository_changelog_opens_with_unreleased():
    """Entries go under it; right after a release it is empty, and that is fine here."""
    changelog.unreleased_notes((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))


def test_cut_names_the_notes_and_opens_an_empty_unreleased_above_them():
    cut = changelog.cut(NOTES, "v1.2.3", "2026-11-18")
    assert cut == (
        "## Unreleased\n\n## v1.2.3 - 2026-11-18\n\n### Fixed — a thing\n\n- It works.\n\n"
        "## v1.2.2 - 2026-11-17\n\n- Older.\n"
    )
    assert changelog.unreleased_notes(cut).strip() == ""


@pytest.mark.parametrize("text, words", [
    ("## v1.2.2 - 2026-11-17\n\n- Older.\n", "must open with"),
    ("# Changelog\n\n## Unreleased\n\n- x\n", "must open with"),
    ("## Unreleased\n\n## v1.2.2 - 2026-11-17\n\n- Older.\n", "is empty"),
    ("## Unreleased\n\n   \n", "is empty"),
])
def test_a_release_without_notes_is_refused(text, words):
    with pytest.raises(ValueError, match=words):
        changelog.cut(text, "v1.2.3", "2026-11-18")


def test_subsections_belong_to_the_notes():
    assert "### Fixed" in changelog.unreleased_notes(NOTES)


def test_main_cuts_the_file_and_refuses_bad_input(tmp_path, monkeypatch, capsys):
    path = tmp_path / "CHANGELOG.md"
    path.write_text(NOTES, encoding="utf-8")
    monkeypatch.setattr(changelog, "CHANGELOG", path)

    assert changelog.main(["check"]) == 0
    assert changelog.main(["cut", "v1.2.3", "2026-11-18"]) == 0
    assert path.read_text(encoding="utf-8").startswith("## Unreleased\n\n## v1.2.3 - 2026-11-18\n")
    assert changelog.main(["check"]) == 1
    assert "is empty" in capsys.readouterr().err
    assert changelog.main(["cut", "v1.2.3"]) == 2


def test_release_sh_checks_before_the_gates_and_cuts_and_commits_after_the_bump():
    release = (REPO_ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    check = release.index("changelog_release.py check")
    gates = release.index("make check\n")
    bump = release.index("make check-versions")
    cut = release.index('changelog_release.py cut "$VERSION"')
    commit = release.index('git commit -m "chore: release $VERSION"')
    assert check < gates < bump < cut < commit
    assert "ui/package.json CHANGELOG.md" in release[cut:commit]
