"""Every Lambda test suite runs in CI and in `make test-lambdas`.

CI lists each Lambda's suite as its own step, so a new suite is silently
skipped until someone adds that step. The coordination, query_subscriptions
and bucket_cleanup suites were all skipped that way.
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LAMBDAS = REPO_ROOT / "sam" / "lambdas"


def _suites():
    for lambda_dir in sorted(p for p in LAMBDAS.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
        if (lambda_dir / "tests").is_dir() or any(lambda_dir.glob("test_*.py")):
            yield lambda_dir.name


def _make_target(name: str) -> str:
    text = (REPO_ROOT / "Makefile").read_text()
    match = re.search(rf"^{re.escape(name)}:.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, f"Makefile has no `{name}` target"
    return match.group(0)


@pytest.mark.parametrize("suite", list(_suites()))
def test_ci_runs_the_suite(suite):
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert f"cd sam/lambdas/{suite} &&" in ci, f"add a `Run {suite} tests` step to the lambdas job in ci.yml"


@pytest.mark.parametrize("suite", list(_suites()))
def test_make_test_lambdas_runs_the_suite(suite):
    assert f"sam/lambdas/{suite} &&" in _make_target("test-lambdas"), f"add {suite} to `make test-lambdas`"


def test_the_suites_are_found():
    assert {"console_api", "coordination"} <= set(_suites())
