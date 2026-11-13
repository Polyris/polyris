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
TASKS_ROUTE = "sam/lambdas/console_api/routes/tasks.py"
EXECUTIONS_ROUTE = "sam/lambdas/console_api/routes/executions.py"


class Edit(NamedTuple):
    path: str          # repo-relative file
    old: str           # exact text, which must occur once
    new: str


class Probe(NamedTuple):
    defect: str
    edits: Tuple[Edit, ...]
    scenario: str      # pytest node id that must fail under the probe


AND_AS_OR = Edit(CORE, "        ready = all(ok for ok, _ in outcomes)\n",
                 "        ready = any(ok for ok, _ in outcomes)\n")

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
    Probe("B-M1", (Edit(TASKS_ROUTE, "    partition_key = item.get('date')\n",
                        "    partition_key = datetime.now(timezone.utc).strftime('%Y-%m-%d')\n"),),
          f"{SCENARIOS}::test_mark_success_writes_the_fact_for_the_marked_runs_partition"),
    # The old resume never asked the reconciler: tasks parked during the pause stayed parked.
    Probe("B-P1", (Edit(EXECUTIONS_ROUTE, "            'reconcile': _reconcile_run(pipeline_execution),\n",
                        "            'reconcile': {},\n"),),
          f"{SCENARIOS}::test_an_asset_arriving_during_a_pause_wakes_the_task_after_resume"),
    # Two guards: the reconciler parks only a task that is otherwise ready, and resume signals
    # nothing without asking it. The old code parked any task whose deps finished during the
    # pause and signalled every parked task deps_ready through its wait_token; the probe does both.
    Probe("B-P2", (
        Edit("sam/lambdas/coordination/coordination_core/decide.py",
             "    if reasons:\n        return Decision(WAIT, tuple(reasons), deps, assets)\n"
             "    if paused:\n        return Decision(PAUSED, (\"pipeline is paused\",), deps, assets)\n",
             "    if paused:\n        return Decision(PAUSED, (\"pipeline is paused\",), deps, assets)\n"
             "    if reasons:\n        return Decision(WAIT, tuple(reasons), deps, assets)\n"),
        Edit(EXECUTIONS_ROUTE, "        if item.get('status') == 'waiting_paused' and item.get('pause_token')\n",
             "        if item.get('status') == 'waiting_paused' and (item.get('pause_token') or item.get('wait_token'))\n"),
        Edit(EXECUTIONS_ROUTE,
             "                sfn.send_task_success(taskToken=item['pause_token'], output=json.dumps({'resumed': True}))\n",
             "                sfn.send_task_success(taskToken=item.get('pause_token') or item['wait_token'],\n"
             "                                      output=json.dumps({'signal': 'deps_ready', 'reason': 'resume'}))\n"),
    ), f"{SCENARIOS}::test_resume_does_not_run_a_task_whose_assets_are_missing"),
    # The old warning rows were found by partition date, not by write time: older ones never reached the bell.
    # The old pause waiter saved the token without an attention entry, so only resume could release it.
    Probe("B-P3", (
        Edit("sam/sfn_templates/helpers/pause_waiter/sfn.tpl.json",
             "paused_at = :paused_at, attention = :attention, attention_at = :due, attempts = :zero",
             "paused_at = :paused_at, attempts = :zero"),
        Edit("sam/sfn_templates/helpers/pause_waiter/sfn.tpl.json",
             "          \":attention\": {\n"
             "            \"S\": \"{% 'paused#' & $substring($hash($states.input.execution_name, 'SHA-256'), 0, 1) %}\"\n"
             "          },\n"
             "          \":due\": {\n"
             "            \"S\": \"{% $fromMillis($toMillis($now()) + 600000) %}\"\n"
             "          },\n",
             ""),
    ), f"{SCENARIOS}::test_a_task_that_saves_its_pause_token_while_resume_runs_is_released_by_the_sweeper"),
    # Without its Catch, a pause waiter that cannot save the token just fails: nobody releases the task.
    Probe("B-P4", (Edit("sam/sfn_templates/helpers/pause_waiter/sfn.tpl.json",
                        ',\n'
                        '      "Catch": [\n'
                        '        {\n'
                        '          "ErrorEquals": [\n'
                        '            "States.ALL"\n'
                        '          ],\n'
                        '          "Comment": "Without a saved token nothing can release the task, and'
                        ' it would run when its 12-hour pause heartbeat ran out, paused or not. Fail its'
                        ' pause wait instead, with the cause.",\n'
                        '          "Output": "{% $merge([$states.input, {\'save_error\':'
                        ' $states.errorOutput}]) %}",\n'
                        '          "Next": "Fail_Pause_Wait"\n'
                        '        }\n'
                        '      ]',
                        ""),),
          f"{SCENARIOS}::test_a_task_whose_pause_token_cannot_be_saved_fails_with_the_cause"),
    Probe("B-N1", (Edit("sam/lambdas/coordination/coordination_core/records.py",
                        "        att.KEY: att.attention(att.WARN, execution_name),\n        att.DUE: format_utc(now),\n",
                        ""),),
          f"{SCENARIOS}::test_a_warning_about_an_old_partition_reaches_the_bell"),
    # The old restart rebuilt the wrapper's input from a few saved fields, without wait_for.
    Probe("B-R1", (Edit("sam/sfn_templates/helpers/restart_task/sfn.tpl.json",
                        "$start := $exists($item.starter_input.S) ? $parse($item.starter_input.S) :",
                        "$start := false ? $parse($item.starter_input.S) :"),),
          f"{SCENARIOS}::test_a_restarted_task_is_still_gated_on_its_assets"),
)
