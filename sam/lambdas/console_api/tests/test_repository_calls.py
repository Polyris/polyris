"""Every repository method the console calls exists on the repository.

The Stop route called `pipelines_repo.get_sfn_arn`, which no repository ever
had: a test fake defined it, no test called the route, and every Stop answered
500 (external review, round 2). This reads the code instead of running it, so
a route no test reaches is covered too, in this build and in the paid overlay
when it is checked out next to it.
"""
import ast
from pathlib import Path

import pytest

import dal

CONSOLE = Path(__file__).resolve().parents[1]
OVERLAY = CONSOLE.parents[3] / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee"
REPOSITORIES = {name: type(getattr(dal, name)) for name in dal.__all__}


def _sources():
    roots = [CONSOLE] + ([OVERLAY] if OVERLAY.is_dir() else [])
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            if "tests" not in path.parts and ".aws-sam" not in path.parts:
                yield path


def _calls():
    for path in _sources():
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in REPOSITORIES:
                yield path, node.lineno, node.value.id, node.attr


def test_the_scan_sees_the_repositories_in_use():
    assert {(repo, attr) for _, _, repo, attr in _calls()} >= {("executions_repo", "get"), ("pipelines_repo", "get")}


@pytest.mark.parametrize("repo", sorted(REPOSITORIES))
def test_every_method_the_console_calls_exists(repo):
    missing = [f"{path.relative_to(path.parents[3])}:{line} {name}.{attr}"
               for path, line, name, attr in _calls() if name == repo and not hasattr(REPOSITORIES[repo], attr)]
    assert missing == []
