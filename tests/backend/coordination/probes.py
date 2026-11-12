"""Mutation probes: each re-introduces one defect in the new code (ADR #130, Testing).

A probe names a defect, the edits that bring it back (each an exact text
that must occur once in its file), and the scenario that must then fail.
Some defects are stopped by two guards; their probe removes both. The runner
(`python tests/backend/coordination/run_probes.py`) applies each probe, runs
its scenario, expects it to fail and restores the files. Probes target the
code the harness runs: the templates and the Lambdas' generated
coordination_core. test_meta.py requires one for every defect whose
scenario runs.
"""

from typing import NamedTuple, Tuple

SCENARIOS = "tests/backend/coordination/test_scenarios.py"
CORE = "sam/lambdas/coordination/coordination_core/asset_conditions.py"
REGISTRATION = "sam/sfn_templates/helpers/registration/sfn.tpl.json"


class Edit(NamedTuple):
    path: str          # repo-relative file
    old: str           # exact text, which must occur once
    new: str


class Probe(NamedTuple):
    defect: str
    edits: Tuple[Edit, ...]
    scenario: str      # pytest node id that must fail under the probe


AND_AS_OR = Edit(CORE, "    return all(outcomes) if node.operator == AND else any(outcomes)",
                 "    return any(outcomes)")

PROBES: Tuple[Probe, ...] = (
    Probe("B-G1", (Edit(
        REGISTRATION,
        '"Comment": "No row exists for the sweeper to find: the task must fail now, with the cause.",\n'
        '          "Output": "{% $states.input ~> |$| {\'failure\': $states.errorOutput} | %}",\n'
        '          "Next": "Fail_Task"',
        '"Comment": "No row exists for the sweeper to find: the task must fail now, with the cause.",\n'
        '          "Output": "{% $states.input ~> |$| {\'failure\': $states.errorOutput} | %}",\n'
        '          "Next": "Registered"'),),
        f"{SCENARIOS}::test_a_failed_registration_fails_the_task_with_its_cause"),
    Probe("B-S1", (Edit(
        "sam/sfn_templates/dependency_wrapper/sfn.tpl.json",
        '"Comment": "B-S1: without the skipped row, dependants must not be woken. Fail visibly instead.",\n'
        '          "Output": "{% $states.input ~> |$| {\'error\': $states.errorOutput} | %}",\n'
        '          "Next": "Handle_Failure"',
        '"Comment": "B-S1: without the skipped row, dependants must not be woken. Fail visibly instead.",\n'
        '          "Output": "{% $states.input %}",\n'
        '          "Next": "Auto_Skip_Notify_Dependents"'),),
        f"{SCENARIOS}::test_a_failed_auto_skip_write_does_not_wake_dependants"),
    Probe("B-A1", (AND_AS_OR,), f"{SCENARIOS}::test_and_of_two_assets_wakes_once_after_both_in_every_order"),
    Probe("B-A2", (AND_AS_OR,), f"{SCENARIOS}::test_mixed_and_or_needs_a_and_one_of_b_or_c"),
    Probe("B-A3", (Edit(CORE, "    fresh = newest.event_time >= now - timedelta(hours=hours)", "    fresh = True"),),
          f"{SCENARIOS}::test_an_early_and_member_is_rechecked_for_freshness"),
    # The old check took the latest fact of any partition.
    Probe("B-C1", (Edit(
        CORE,
        "    for fact in merge_facts(reader.facts(name, last=target, first=first), known, first, target):\n"
        "        seen_any = True\n"
        "        if satisfies(fact, target):",
        "    for fact in merge_facts(reader.facts(name, last=date.max, first=first), known, first, date.max):\n"
        "        seen_any = True\n"
        "        if True:"),),
        f"{SCENARIOS}::test_a_backfill_does_not_pass_on_todays_fact"),
    # The old pointers were keyed by task name, shared by every run and pipeline.
    Probe("B-K1", (Edit(REGISTRATION, "'subscriber': $me, 'ttl': $ttl} }); $assets",
                        "'subscriber': $states.input.task_name, 'ttl': $ttl} }); $assets"),
                   Edit(REGISTRATION, "{'dependency_key': 'asset:' & $a, 'subscriber': $me,",
                        "{'dependency_key': 'asset:' & $a, 'subscriber': $states.input.task_name,")),
          f"{SCENARIOS}::test_same_named_waiting_tasks_in_two_pipelines_both_wake"),
    Probe("B-T1", (Edit("sam/lambdas/coordination/reconcile.py",
                        '        if status not in RECONCILABLE or row.get("pause_token"):',
                        '        if row.get("pause_token"):'),),
          f"{SCENARIOS}::test_a_repeated_hint_does_not_disturb_a_running_task"),
    # The old AND trigger counted events: every event could start another run.
    Probe("B-U1", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '        every_event = fact.get("cascade") == "all" or schedule.operator == sp.OR',
                        "        every_event = True"),),
          f"{SCENARIOS}::test_push_and_starts_exactly_one_run_for_the_day"),
    # The old AND run names ended in the constant `-asset-and`.
    Probe("B-X1", (Edit("sam/lambdas/coordination/coordination_core/run_name.py",
                        '    tail = f"-{_clean(date)}-{kind}-{_digest(pipeline, date, kind, discriminator, length=8)}"',
                        '    tail = f"-{_clean(date)}-asset-and"'),),
          f"{SCENARIOS}::test_two_push_consumers_with_the_same_task_id_on_one_day_both_run"),
    # Three guards: notify_dependents swallows a failed query, run_task does not wait for its
    # result, and run_task's catch only warns. The probe couples the producer to all three again.
    Probe("B-F1", (
        Edit("sam/sfn_templates/helpers/notify_dependents/sfn.tpl.json",
             '            "States.ALL"\n          ],\n          "Comment": "Hints lost; the sweeper re-checks the waiting tasks.",',
             '            "Probe.NeverMatches"\n          ],\n          "Comment": "Hints lost; the sweeper re-checks the waiting tasks.",'),
        Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
             '            "Comment": "Notify waiting tasks via SFN helper (sync - ensure delivery)",\n'
             '            "Resource": "arn:aws:states:::aws-sdk:sfn:startSyncExecution",',
             '            "Comment": "Notify waiting tasks via SFN helper (sync - ensure delivery)",\n'
             '            "Resource": "arn:aws:states:::states:startExecution.sync:2",'),
        Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
             '                    "Output": "{% $states.input %}",\n                    "Next": "Warn_Notify_Dependents_Failed"',
             '                    "Output": "{% $states.input %}",\n                    "Next": "Notify_Dependents_Failed"'),
    ), f"{SCENARIOS}::test_a_failed_fan_out_does_not_fail_the_producer"),
)
