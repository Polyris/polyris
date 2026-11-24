"""The Lambdas that import polyris pin the same release, and release.sh moves every pin (ADR #102, ADR #130 §10).

The coordination Lambdas and the console import polyris.coordination from the SDK their requirements.txt
pins, so a pin left behind would run older coordination code than the platform release it ships with.
"""

import re
from pathlib import Path

import pytest

from polyris.platform import is_downgrade

REPO = Path(__file__).resolve().parents[2]
LAMBDAS = ("console_api", "coordination")
PIN = re.compile(r"^polyris @ git\+https://github\.com/Polyris/polyris@v(\d+\.\d+\.\d+)$", re.M)


def _pin(name: str) -> str:
    text = (REPO / "sam" / "lambdas" / name / "requirements.txt").read_text()
    match = PIN.search(text)
    assert match, f"sam/lambdas/{name}/requirements.txt must pin polyris to a release tag"
    return match.group(1)


def test_every_lambda_pins_the_release_this_repo_is_at():
    """A pin names a release tag. Between releases the repo may carry a pre-release version
    (1.3.0rc1), which has no tag to pin: the pins then stay together on the last release, and
    release.sh moves both to the release."""
    version = re.search(r'^version = "(.+)"', (REPO / "pyproject.toml").read_text(), re.M).group(1)
    pins = {name: _pin(name) for name in LAMBDAS}
    if re.fullmatch(r"\d+\.\d+\.\d+", version):
        assert pins == {name: version for name in LAMBDAS}
    else:
        assert len(set(pins.values())) == 1, f"the Lambdas pin different releases: {pins}"
        pinned = pins[LAMBDAS[0]]
        assert is_downgrade(version, pinned), f"the pins name v{pinned}, which is no release before {version}"


@pytest.mark.parametrize("name", LAMBDAS)
def test_release_moves_and_commits_every_pin(name):
    release = (REPO / "scripts" / "release.sh").read_text()
    assert release.count(f"sam/lambdas/{name}/requirements.txt") == 2, \
        "the sed that moves it, and RELEASE_FILES (committed, and put back on a failure)"
