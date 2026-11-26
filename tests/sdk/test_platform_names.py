"""The platform's state machines and functions are named by one constant, ``platform.PLATFORM_NAMES``.

The template names them, and every deny list of a role or policy that a pipeline or its author holds
names them again, as patterns that match the platform of any namespace in the account. A function or
state machine that joins the template joins the constant, and then the deny lists that derive from it,
or this fails (Principle #28).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from polyris.platform import PLATFORM_NAMES, platform_prefix

REPO_ROOT = Path(__file__).resolve().parents[2]
PREFIX = platform_prefix("${Namespace}", "${Stage}")


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


def _names(kind: str, key: str) -> list:
    """The end of the name, after the platform's prefix, of every resource of the template of type ``kind``."""
    names = []
    for resource in _template()["Resources"].values():
        if resource["Type"] == kind:
            name = resource["Properties"][key]
            assert isinstance(name, str) and name.startswith(PREFIX), name
            names.append(name[len(PREFIX):])
    return names


def test_the_constant_names_every_function_of_the_template():
    functions = _names("AWS::Serverless::Function", "FunctionName") + _names("AWS::Lambda::Function", "FunctionName")
    assert sorted(functions) == sorted(PLATFORM_NAMES.functions)


def test_the_constant_names_every_state_machine_of_the_template():
    machines = [name for kind, key in (("AWS::Serverless::StateMachine", "Name"),
                                  ("AWS::StepFunctions::StateMachine", "StateMachineName"))
                for name in _names(kind, key)]
    assert sorted(machines) == sorted(PLATFORM_NAMES.state_machines)


def test_the_groups_of_the_constant_do_not_overlap():
    assert not set(PLATFORM_NAMES.pipeline_functions) & set(PLATFORM_NAMES.internal_functions)
    assert not set(PLATFORM_NAMES.engine_machines) & set(PLATFORM_NAMES.test_machines)
