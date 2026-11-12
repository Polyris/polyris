"""Every test suite runs in CI and in `make test`.

CI lists each Lambda's suite as its own step and calls one make target per
pytest directory, so a new suite or directory is silently skipped until
someone wires it in. The coordination, query_subscriptions and bucket_cleanup
suites were skipped that way, and so was tests/docs, though a comment in
ci.yml said it ran.
"""

import re
import tomllib
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


def _test_dirs():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    return pyproject["tool"]["pytest"]["ini_options"]["testpaths"]


def _targets_running(test_dir: str):
    text = (REPO_ROOT / "Makefile").read_text()
    for match in re.finditer(r"^([a-z][\w-]*):.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL):
        if re.search(rf"pytest[^\n]*\b{re.escape(test_dir)}/?(\s|$)", match.group(0)):
            yield match.group(1)


@pytest.mark.parametrize("test_dir", _test_dirs())
def test_ci_runs_every_test_dir(test_dir):
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    targets = list(_targets_running(test_dir))
    assert targets, f"no make target runs {test_dir}; add one"
    assert any(re.search(rf"run: make {re.escape(t)}\s*$", ci, re.MULTILINE) for t in targets), (
        f"ci.yml runs none of {targets}, so {test_dir} never runs in CI")


@pytest.mark.parametrize("test_dir", _test_dirs())
def test_make_test_runs_every_test_dir(test_dir):
    prerequisites = _make_target("test").split(":", 1)[1].split()
    assert set(_targets_running(test_dir)) & set(prerequisites), f"add the target that runs {test_dir} to `make test`"


def test_the_test_dirs_are_found():
    assert {"tests/sdk", "tests/docs"} <= set(_test_dirs())
