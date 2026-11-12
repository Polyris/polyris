"""Mutation probes: each re-introduces one defect in the new code (ADR #130, Testing).

A probe is (defect id, file, old text, new text, scenario node id). The
runner (`python tests/backend/coordination/run_probes.py`) applies each
probe, runs its scenario, expects it to fail, and restores the file.
Probes are added as their scenarios are bound; test_meta.py requires one for
every defect whose scenario runs.
"""

from typing import NamedTuple, Tuple

SCENARIOS = "tests/backend/coordination/test_scenarios.py"


class Probe(NamedTuple):
    defect: str
    path: str          # repo-relative file the probe mutates
    old: str           # exact text, which must occur once
    new: str
    scenario: str      # pytest node id that must fail under the probe


PROBES: Tuple[Probe, ...] = (
    Probe(
        "B-G1",
        "sam/sfn_templates/helpers/registration/sfn.tpl.json",
        '"Comment": "No row exists for the sweeper to find: the task must fail now, with the cause.",\n'
        '          "Output": "{% $states.input ~> |$| {\'failure\': $states.errorOutput} | %}",\n'
        '          "Next": "Fail_Task"',
        '"Comment": "No row exists for the sweeper to find: the task must fail now, with the cause.",\n'
        '          "Output": "{% $states.input ~> |$| {\'failure\': $states.errorOutput} | %}",\n'
        '          "Next": "Registered"',
        f"{SCENARIOS}::test_a_failed_registration_fails_the_task_with_its_cause",
    ),
    Probe(
        "B-S1",
        "sam/sfn_templates/dependency_wrapper/sfn.tpl.json",
        '"Comment": "B-S1: without the skipped row, dependants must not be woken. Fail visibly instead.",\n'
        '          "Output": "{% $states.input ~> |$| {\'error\': $states.errorOutput} | %}",\n'
        '          "Next": "Handle_Failure"',
        '"Comment": "B-S1: without the skipped row, dependants must not be woken. Fail visibly instead.",\n'
        '          "Output": "{% $states.input %}",\n'
        '          "Next": "Auto_Skip_Notify_Dependents"',
        f"{SCENARIOS}::test_a_failed_auto_skip_write_does_not_wake_dependants",
    ),
)
