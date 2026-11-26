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


# ── Every deny list names the platform's machines and functions as the constant does ──────────────────


def _machines(names, *, executions=()) -> list:
    """What a deny list holds for these state machines, of the platform of any namespace in the account
    (D9): each machine, qualified (``:*``) too, and the executions of those named in ``executions``."""
    arns = [f"arn:aws:states:*:*:stateMachine:*-polyris-{n}{q}" for n in names for q in ("", ":*")]
    return sorted(arns + [f"arn:aws:states:*:*:execution:*-polyris-{n}:*" for n in executions])


def _deny(resource: str, sid: str) -> dict:
    properties = _template()["Resources"][resource]["Properties"]
    documents = ([properties["PolicyDocument"]] if "PolicyDocument" in properties
                 else [p["PolicyDocument"] for p in properties["Policies"]])
    found = [s for doc in documents for s in doc["Statement"] if s.get("Sid") == sid]
    assert len(found) == 1, f"{resource} has {len(found)} statements {sid}"
    assert found[0]["Effect"] == "Deny"
    return found[0]


def test_the_roles_a_pipeline_runs_as_invoke_none_of_the_platforms_internal_functions():
    expected = sorted(f"arn:aws:lambda:*:*:function:*-polyris-{n}{q}"
                      for n in PLATFORM_NAMES.internal_functions for q in ("", ":*"))
    for policy in ("OrchestrationRolePolicy", "CrossAccountPipelinePolicy"):
        assert sorted(_deny(policy, "NeverThePlatformsFunctions")["Resource"]) == expected, policy


def test_the_roles_a_pipeline_runs_as_start_stop_and_redrive_none_of_the_engines_machines():
    expected = _machines(PLATFORM_NAMES.engine_machines, executions=PLATFORM_NAMES.engine_machines)
    for policy in ("OrchestrationRolePolicy", "CrossAccountPipelinePolicy"):
        statement = _deny(policy, "NeverTheEngineMachines")
        assert sorted(statement["Resource"]) == expected, policy
        assert set(statement["Action"]) == {"states:StartExecution", "states:StopExecution",
                                            "states:RedriveExecution"}


def test_the_schedule_role_starts_none_of_the_platforms_machines():
    assert sorted(_deny("PipelineScheduleRole", "NeverThePlatformsMachines")["Resource"]) == _machines(
        PLATFORM_NAMES.state_machines)


def test_the_deploy_policy_touches_none_of_the_platforms_machines():
    statement = _deny("PipelineDeployPolicy", "NeverThePlatformsMachines")
    assert sorted(statement["Resource"]) == _machines(PLATFORM_NAMES.state_machines,
                                                      executions=PLATFORM_NAMES.engine_machines)
    assert "states:CreateStateMachine" in statement["Action"], "no pipeline may take a platform machine's name"


def test_the_console_starts_no_run_task():
    statement = _deny("ConsoleApiRole", "NeverStartRunTask")
    assert sorted(statement["Resource"]) == _machines((PLATFORM_NAMES.run_task,))
