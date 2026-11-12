"""Every Step Functions call the console API makes is allowed by ConsoleApiRole.

`/api/health` called `states:ListStateMachines`, which the role never granted,
so every deployed stack reported "unhealthy" (found in the ADR #131 live
smoke). IAM can't be type-checked, so this pins the contract: each
`sfn.<operation>(…)` / `sfn.get_paginator('<operation>')` in production
console_api code needs `states:<Operation>` on the role (Principle #28).
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_API = REPO_ROOT / "sam" / "lambdas" / "console_api"

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


def _used_operations() -> dict[str, list[str]]:
    used: dict[str, list[str]] = {}
    for path in CONSOLE_API.rglob("*.py"):
        if "tests" in path.relative_to(CONSOLE_API).parts:
            continue
        text = path.read_text()
        ops = set(_CALL_RE.findall(text)) - _NOT_OPERATIONS
        ops |= set(_PAGINATOR_RE.findall(text))
        for op in ops:
            used.setdefault(op, []).append(str(path.relative_to(REPO_ROOT)))
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
