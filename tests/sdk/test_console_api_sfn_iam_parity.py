"""ConsoleApiRole grants exactly the Step Functions calls the console API makes.

`/api/health` called `states:ListStateMachines`, which the role never granted,
so every deployed stack reported "unhealthy" (found in the ADR #131 live
smoke). IAM can't be type-checked, so this pins the contract both ways: each
`sfn.<operation>(…)` / `sfn.get_paginator('<operation>')` in production
console_api code needs `states:<Operation>` on the role (Principle #28), and
the role grants no Step Functions action that nothing calls: it answered task
tokens and listed executions long after the console stopped doing either (the
final review of TR-6, P-25). The paid overlay runs under the same role, so its
calls count too, read from it when it is checked out next to this repository.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_API = REPO_ROOT / "sam" / "lambdas" / "console_api"
OVERLAY = REPO_ROOT.parent / "polyris-ee" / "sam" / "lambdas" / "console_api" / "ee"
#: What the paid overlay calls through the role, for a build without it checked out; when it is, the test below
#: holds the list to its code.
OVERLAY_OPERATIONS = {"describe_execution", "start_execution"}

_CALL_RE = re.compile(r"\bsfn\.([a-z_]+)\(")
_PAGINATOR_RE = re.compile(r"\bsfn\.get_paginator\(\s*['\"]([a-z_]+)['\"]")
# Client methods that are not API operations.
_NOT_OPERATIONS = {"get_paginator", "get_waiter", "can_paginate", "close"}


def _template() -> dict:
    class _Loader(yaml.SafeLoader):
        pass

    def _tag(loader, node):
        if isinstance(node, yaml.ScalarNode):
            return loader.construct_scalar(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_mapping(node)

    for tag in ("!Ref", "!Sub", "!GetAtt", "!Join", "!If", "!Not", "!Equals", "!And", "!Or",
                "!Condition", "!Select", "!Split", "!ImportValue", "!FindInMap"):
        _Loader.add_constructor(tag, _tag)
    return yaml.load((REPO_ROOT / "sam" / "template.yaml").read_text(), Loader=_Loader)


def _granted_states_actions() -> set[str]:
    role = _template()["Resources"]["ConsoleApiRole"]["Properties"]
    actions: set[str] = set()
    for policy in role.get("Policies", []):
        for stmt in policy["PolicyDocument"]["Statement"]:
            if stmt.get("Effect") != "Allow":
                continue
            listed = stmt.get("Action", [])
            actions.update(a for a in ([listed] if isinstance(listed, str) else listed) if a.startswith("states:"))
    return actions


def _used_operations(root: Path = CONSOLE_API) -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in root.rglob("*.py"):
        if "tests" in path.relative_to(root).parts or ".aws-sam" in path.parts:
            continue
        text = path.read_text()
        ops = set(_CALL_RE.findall(text)) - _NOT_OPERATIONS
        ops |= set(_PAGINATOR_RE.findall(text))
        for op in ops:
            used.setdefault(op, []).append(str(path.relative_to(root.parent)))
    return used


def _action(operation: str) -> str:
    return "states:" + "".join(part.capitalize() for part in operation.split("_"))


def test_every_console_api_sfn_call_is_granted():
    granted = _granted_states_actions()
    if "states:*" in granted:  # pragma: no cover — would make the check vacuous
        return
    missing = {_action(op): files for op, files in _used_operations().items() if _action(op) not in granted}
    assert not missing, f"ConsoleApiRole lacks IAM actions used by console_api: {missing}"


def test_scanner_sees_the_health_check():
    """Guard against the scanner silently matching nothing."""
    assert "list_state_machines" in _used_operations()
    assert _action("list_state_machines") == "states:ListStateMachines"


def test_the_role_grants_no_step_functions_action_that_nothing_calls():
    called = {_action(op) for op in set(_used_operations()) | OVERLAY_OPERATIONS}
    extra = sorted(_granted_states_actions() - called)
    assert not extra, f"ConsoleApiRole grants Step Functions actions nothing calls: {extra}"


def test_the_overlay_s_operations_are_the_ones_its_code_calls():
    if not OVERLAY.is_dir():
        pytest.skip("the paid overlay is not checked out next to this repository")
    assert set(_used_operations(OVERLAY)) == OVERLAY_OPERATIONS


def _statements() -> list:
    role = _template()["Resources"]["ConsoleApiRole"]["Properties"]
    return [stmt for policy in role.get("Policies", []) for stmt in policy["PolicyDocument"]["Statement"]]


def test_the_console_starts_only_the_platforms_state_machines_and_never_run_task():
    """The console started any state machine of the account (IAM_HOLES_VERIFIED, claim 6c). Its Run starts a
    registered pipeline, the overlay's backfill bulk_backfill; run_task is the dispatcher's alone, of any
    platform in the account."""
    from polyris.platform import PLATFORM_NAMES

    starts = [s for s in _statements() if "states:StartExecution" in ([s["Action"]] if isinstance(s["Action"], str)
                                                                       else s["Action"])]
    allow = [s for s in starts if s["Effect"] == "Allow"]
    deny = [s for s in starts if s["Effect"] == "Deny"]
    assert [s["Resource"] for s in allow] == ["arn:aws:states:${AWS::Region}:${AWS::AccountId}:stateMachine:*-polyris-*"]
    run_task = f"arn:aws:states:*:*:stateMachine:*-polyris-{PLATFORM_NAMES.run_task}"
    assert [sorted(s["Resource"]) for s in deny] == [[run_task, f"{run_task}:*"]]
