"""Mutation probes: each re-introduces one defect in the new code (ADR #130, Testing).

A probe names a defect, the edits that bring it back (each an exact text
that must occur once in its file), and the scenario that must then fail.
Some defects are stopped by two guards; their probe removes both. The runner
(`python tests/backend/coordination/run_probes.py`) applies each probe, runs
its scenario, expects it to fail and restores the files. Probes target the
code the harness runs: the templates, the Lambdas and the coordination core
(polyris/coordination), which the Lambdas import. test_meta.py requires one for every defect whose
scenario runs.
"""

from typing import NamedTuple, Tuple

SCENARIOS = "tests/backend/coordination/test_scenarios.py"
ROWS = "tests/backend/coordination/test_scenarios_rows.py"
DISPATCHER = "sam/lambdas/coordination/dispatcher.py"
CORE = "polyris/coordination/asset_conditions.py"
REGISTRATION = "sam/sfn_templates/helpers/registration/sfn.tpl.json"
TASKS_ROUTE = "sam/lambdas/console_api/routes/tasks.py"
EXECUTIONS_ROUTE = "sam/lambdas/console_api/routes/executions.py"
RUN_TASK = "sam/sfn_templates/helpers/run_task/sfn.tpl.json"


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
    Probe("B-K1", (Edit("polyris/coordination/start_run.py",
                        "            subscriber = task_row.key(task_id, call.date, call.execution)\n",
                        "            subscriber = task_id\n"),),
          f"{SCENARIOS}::test_same_named_waiting_tasks_in_two_pipelines_both_wake"),
    # The old AND trigger counted events: every event could start another run.
    Probe("B-U1", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '        every_event = fact.get("cascade") == "all" or schedule.operator == sp.OR',
                        "        every_event = True"),),
          f"{SCENARIOS}::test_push_and_starts_exactly_one_run_for_the_day"),
    # The old AND run names ended in the constant `-asset-and`.
    Probe("B-X1", (Edit("polyris/coordination/run_name.py",
                        '    tail = f"-{_clean(date)}-{kind}-{_digest(pipeline, date, kind, discriminator, length=8)}"',
                        '    tail = f"-{_clean(date)}-asset-and"'),),
          f"{SCENARIOS}::test_two_push_consumers_with_the_same_task_id_on_one_day_both_run"),
    Probe("B-M1", (Edit(TASKS_ROUTE, "    partition_key = item.get('date')\n",
                        "    partition_key = datetime.now(timezone.utc).strftime('%Y-%m-%d')\n"),),
          f"{SCENARIOS}::test_mark_success_writes_the_fact_for_the_marked_runs_partition"),
    # The old resume never asked the reconciler: tasks parked during the pause stayed parked.
    Probe("B-P1", (Edit(EXECUTIONS_ROUTE, "            'reconcile': _reconcile_run(pipeline_execution),\n",
                        "            'reconcile': {},\n"),),
          f"{SCENARIOS}::test_an_asset_arriving_during_a_pause_wakes_the_task_after_resume"),
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
    # Without its catcher, a pause waiter that cannot save the token just fails: nobody releases the task.
    Probe("B-P4", (Edit("sam/sfn_templates/helpers/pause_waiter/sfn.tpl.json",
                        ',\n'
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
                        '        }',
                        ""),),
          f"{SCENARIOS}::test_a_task_whose_pause_token_cannot_be_saved_fails_with_the_cause"),
    # The old registration replaced the row whatever it held.
    Probe("B-O1", (Edit("polyris/coordination/ownership.py",
                        'RAISE = "generation = if_not_exists(generation, :zero) + :one"',
                        'RAISE = "generation = if_not_exists(generation, :one) + :zero"'),),
          f"{SCENARIOS}::test_an_operator_skip_holds_when_the_wrapper_could_not_be_stopped"),
    # Without the cutover every consumer subscribed now reacts to every fact the catch-up replays.
    Probe("B-U2", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '        if cutover and parse_utc(str(fact["event_time"])) < parse_utc(str(cutover)):\n',
                        '        if False:\n'),),
          f"{SCENARIOS}::test_a_consumer_deployed_after_the_facts_does_not_start_for_them"),
    # A retried claim fails its own condition: without reading whose claim it was, it ends as superseded.
    Probe("B-W1", (Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
                        '"Next": "Read_Claim"', '"Next": "Superseded"'),),
          f"{SCENARIOS}::test_a_claim_whose_answer_is_lost_still_runs_the_task"),
    # Define_Inputs replaces the whole input: without a computed date the run falls back to the day it started.
    Probe("B-D1", (Edit("polyris/generators.py",
                        '                "current_date": JSONATA_DATE,\n',
                        '                "current_date": None,\n'),),
          f"{SCENARIOS}::test_a_scheduled_run_carries_the_day_it_was_scheduled_for"),
    # Without the scheduled time in the schedule's input, a late start gets the next day.
    Probe("B-D1", (Edit("polyris/deploy.py",
                        '                            SCHEDULED_TIME_FIELD: "<aws.scheduler.scheduled-time>",\n',
                        ''),),
          f"{SCENARIOS}::test_a_scheduled_run_carries_the_day_it_was_scheduled_for"),
    # The old fallback: the clock when the date is computed, which a run that starts at midnight has passed.
    Probe("B-D1", (Edit("polyris/generators.py",
                        "    \": $substringBefore($states.context.Execution.StartTime, 'T')))\"\n",
                        "    \": $substringBefore($now(), 'T')))\"\n"),),
          f"{SCENARIOS}::test_every_task_of_a_run_crossing_midnight_gets_the_day_it_started"),
    # Without the top-level date in the push input and the full chain in Define_Inputs, a pushed run of a
    # pipeline with variables= gets the day it started.
    Probe("B-D1", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '            "current_date": day,\n            "PARTITION_ARG": day,\n', ''),
                   Edit("polyris/generators.py",
                        '                "current_date": JSONATA_DATE,\n',
                        '                "current_date": f"{{% {_RUN_DATE} %}}",\n')),
          f"{SCENARIOS}::test_a_pushed_run_of_a_pipeline_with_variables_carries_the_facts_partition"),
    # The old hand-over kept the finished status until the new wrapper registered.
    Probe("B-R2", (Edit("sam/sfn_templates/helpers/restart_task/sfn.tpl.json",
                        '"SET generation = if_not_exists(generation, :zero) + :one, #s = :stopped REMOVE',
                        '"SET generation = if_not_exists(generation, :zero) + :one, #s = if_not_exists(#s, :stopped) REMOVE'),),
          f"{SCENARIOS}::test_a_restart_never_lets_a_dependant_run_on_the_old_status"),
    # Without the drop, a subscription the pipeline no longer has stays and is evaluated for every fact.
    Probe("B-U3", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '            self._drop_subscription(row, "the pipeline is no longer scheduled on this asset")\n',
                        '            pass\n'),),
          f"{SCENARIOS}::test_an_asset_dropped_from_the_schedule_no_longer_starts_the_pipeline"),
    # Registration writes "{}" for a pipeline without an asset schedule; read as unreadable, it warns on every fact.
    Probe("B-U3", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        '            if not value:\n                return None, False\n',
                        ''),),
          f"{SCENARIOS}::test_a_pipeline_moved_to_a_cron_loses_its_push_subscription_quietly"),
    # A subscription time that moves on every registration hides the facts a lost stream record left behind.
    Probe("B-U2", (Edit("polyris/generators.py",
                        '"subscribed_at = if_not_exists(subscribed_at, :now)"',
                        '"subscribed_at = :now"'),),
          f"{SCENARIOS}::test_a_consumer_registered_again_keeps_its_subscription_time"),
    # The old model: a wrapper retried its wait and re-registered over whatever row another wrapper owned.
    Probe("B-O2", (Edit("sam/sfn_templates/dependency_wrapper/sfn.tpl.json",
                        '"StepFunctions.ExecutionLimitExceeded"\n          ],\n          "IntervalSeconds": 5,',
                        '"StepFunctions.ExecutionLimitExceeded",\n            "States.Timeout"\n          ],\n'
                        '          "IntervalSeconds": 5,'),
                   Edit(REGISTRATION,
                        '"ConditionExpression": "attribute_not_exists(execution_name) OR ((generation = :g OR '
                        'attribute_not_exists(generation)) AND (wrapper_execution_arn <> :self OR #s IN (:waiting, :paused)))",',
                        '"ConditionExpression": "attribute_not_exists(execution_name) OR generation = :g OR '
                        'wrapper_execution_arn <> :self OR #s IN (:waiting, :paused)",')),
          f"{SCENARIOS}::test_a_wrapper_a_restart_replaced_never_takes_the_row_back"),
    # Waking a superseded wrapper with deps_ready: a pre-upgrade wrapper has no claim to fail.
    Probe("B-O3", (Edit(REGISTRATION,
                        '"Resource": "arn:aws:states:::aws-sdk:sfn:sendTaskFailure",\n'
                        '      "Arguments": {\n'
                        '        "TaskToken": "{% $states.input.wait_token %}",\n'
                        '        "Error": "Superseded",',
                        '"Resource": "arn:aws:states:::aws-sdk:sfn:sendTaskSuccess",\n'
                        '      "Arguments": {\n'
                        '        "TaskToken": "{% $states.input.wait_token %}",\n'
                        '        "Output": "{\\"signal\\":\\"deps_ready\\",\\"reason\\":\\"superseded\\"}",\n'
                        '        "Error": "Superseded",'),),
          f"{SCENARIOS}::test_a_pre_upgrade_wrapper_never_runs_a_task_an_operator_decided"),
    # run_task's running write without the generation fence takes the row back from an operator's decision.
    # A run_task whose generation an operator raised claims the row of the newer generation (Rule 47).
    Probe("B-O4", (Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
                        '"ConditionExpression": "#s = :deps_ready AND #g = :g",',
                        '"ConditionExpression": "#s = :deps_ready AND (#g = :g OR attribute_exists(execution_name))",'),),
          f"{SCENARIOS}::test_run_task_never_starts_a_task_an_operator_decided_after_its_wait"),
    # The pause wait's heartbeat retry saved its token over an operator's decision, unconditionally.
    Probe("B-O5", (Edit("sam/sfn_templates/helpers/pause_waiter/sfn.tpl.json",
                        '"ConditionExpression": "(generation = :g OR attribute_not_exists(generation))",',
                        '"ConditionExpression": "(generation = :g OR attribute_exists(execution_name))",'),),
          f"{SCENARIOS}::test_a_task_an_operator_decided_while_paused_is_never_released_to_run"),
    # Without the warning a fact Mark Successful could not record leaves no trace but a log line.
    Probe("B-M2", (Edit("sam/lambdas/console_api/routes/tasks.py",
                        "        _warn_fact_not_recorded(item, task_name, asset_name, error)\n",
                        "        pass\n"),),
          f"{SCENARIOS}::test_a_fact_mark_success_cannot_record_is_reported_and_restart_records_it"),
    # Without the guard an unreadable fact raises out of the reader and fails the waiting task.
    Probe("B-F2", (Edit("sam/lambdas/coordination/stores.py",
                        "            return self.registry.fact(item)\n        except ValueError as exc:\n",
                        "            return self.registry.fact(item)\n        except KeyError as exc:\n"),),
          f"{SCENARIOS}::test_an_unreadable_fact_fails_no_waiting_task"),
    Probe("B-N1", (Edit("polyris/coordination/records.py",
                        "        att.KEY: att.attention(att.WARN, execution_name),\n        att.DUE: format_utc(now),\n",
                        ""),),
          f"{SCENARIOS}::test_a_warning_about_an_old_partition_reaches_the_bell"),
    # A wait for facts that never ends: the task waits on, and so does its run.
    Probe("B-Q1", (Edit("polyris/coordination/deadlines.py", "WAIT_FOR_LIMIT = timedelta(days=7)\n",
                        "WAIT_FOR_LIMIT = timedelta(days=7000)\n"),),
          f"{SCENARIOS}::test_a_dependency_wait_that_times_out_fails_the_task_once"),
    # ADR #135: the flag that told the failure handler run_task had answered, and its catch.
    Probe("B-RN1", (Edit("sam/sfn_templates/helpers/failure_handler/sfn.tpl.json",
                         "\"Condition\": \"{% $exists($states.input.orchestration_token) and $states.input.orchestration_token != '' %}\"",
                         "\"Condition\": \"{% $exists($states.input.orchestration_token) and $states.input.orchestration_token != '' and $not($exists($states.input.helper_failed) and $states.input.helper_failed = true) %}\""),
                    Edit("sam/sfn_templates/dependency_wrapper/sfn.tpl.json",
                         "'task_name': $states.input.task_name} | %}\",\n      \"Catch\": [\n        {\n          \"ErrorEquals\": [\n            \"States.ALL\"\n          ],\n          \"Output\": \"{% $states.input ~> |$| {'error': $states.errorOutput} | %}\"",
                         "'task_name': $states.input.task_name} | %}\",\n      \"Catch\": [\n        {\n          \"ErrorEquals\": [\n            \"States.ALL\"\n          ],\n          \"Output\": \"{% $states.input ~> |$| {'error': $states.errorOutput, 'helper_failed': true} | %}\"")),
          f"{SCENARIOS}::test_a_task_whose_pause_token_cannot_be_saved_fails_with_the_cause"),
    Probe("B-RN4", (Edit("sam/lambdas/console_api/routes/tasks.py",
                         "        executions_repo.put(warning_item(\n            kind='coordination', execution_name=execution_name,",
                         "        (lambda _row: None)(warning_item(\n            kind='coordination', execution_name=execution_name,"),),
          f"{SCENARIOS}::test_a_console_action_whose_run_cannot_be_told_says_so"),
    # A run's answer that failed counts as given: the run stays RUNNING with every row final (§4).
    Probe("R-E1", (Edit(DISPATCHER, "            failures.reraise_transient(exc)\n            self._warn(run, subject=f\"{run.key}-answer\"",
                        "            return True\n            self._warn(run, subject=f\"{run.key}-answer\""),),
          f"{ROWS}::test_a_run_whose_answer_was_lost_is_answered_at_its_next_check"),
    # A succeeded upstream whose output is missing reaches the task as if it had none to give (D109).
    Probe("R-O1", (Edit(RUN_TASK, "'missing': $append([], $states.result[found = false and status = 'success'].name)}",
                        "'missing': []}"),),
          f"{ROWS}::test_a_succeeded_upstream_without_its_output_keeps_its_dependant_from_running"),
    # The state carries the work's result twice, as 1.2's success did: past ~125 KB a success cannot be recorded.
    Probe("R-O2", (Edit(RUN_TASK, "'task_output': $raw, 'task_execution_arn': $exists($states.result.ExecutionArn)",
                        "'task_output': $raw, 'task_result': $states.result.Output, "
                        "'task_execution_arn': $exists($states.result.ExecutionArn)"),),
          f"{ROWS}::test_an_output_near_the_payload_limit_is_recorded_and_one_over_it_waits_for_a_decision"),
    # A repeated wake-up decides a running row again, as 1.2's second signal overwrote running.
    Probe("B-T1", (Edit("polyris/coordination/dispatch.py", "    if row.status in _WAITS:\n        return _wait(",
                        "    if row.status in _WAITS | {S.RUNNING.value}:\n        return _wait("),),
          f"{SCENARIOS}::test_a_repeated_hint_does_not_disturb_a_running_task"),
    # A read DynamoDB left partly unprocessed counts as complete: its unread rows count as missing (fail-open).
    Probe("R-F1", (Edit(DISPATCHER, '                request = response.get("UnprocessedKeys") or {}\n',
                        "                request = {}\n"),),
          f"{ROWS}::test_a_partial_read_starts_nothing_until_the_read_completes"),
    # Without its run row's check a lost stream record leaves its task waiting for good (§7).
    Probe("R-L1", (Edit("sam/lambdas/coordination/sweeper.py", "            run_key = self._run_of(entry)\n",
                        "            run_key = None if run_row.is_run_row(key) else self._run_of(entry)\n"),),
          f"{ROWS}::test_a_lost_hint_heals_at_its_run_rows_next_check_with_one_warning"),
    # An upstream that failed is read like one that succeeded: its date's output# holds an older run's output.
    Probe("R-N2", (Edit(RUN_TASK, "$u.status = 'success' or ($u.status = 'skipped'",
                        "$u.status in ['success', 'failed'] or ($u.status = 'skipped'"),),
          f"{ROWS}::test_a_task_after_a_final_failure_gets_its_status_and_never_an_older_runs_output"),
    # The read parses every stored text: a 1.2 row's string result fails it, and the dependant gets no output.
    Probe("R-J1", (Edit(RUN_TASK, "($substring($trim($raw), 0, 1) in ['{', '[', '\\\"', '-', '0', '1', '2', '3', '4', "
                                  "'5', '6', '7', '8', '9', 't', 'f', 'n'] ? ($p := $parse($raw); $exists($p) ? "
                                  "{'output': $p} : {}) : {'output': $raw})",
                        "($p := $parse($raw); $exists($p) ? {'output': $p} : {})"),),
          f"{ROWS}::test_a_string_result_a_1_2_run_task_stored_reaches_the_dependant_as_that_string"),
    # xcom refuses only a final failure and a rule's skip, as TR-5b did: an upstream still running is read.
    Probe("R-N3", (Edit("polyris/xcom.py",
                        "    if status == _SUCCESS or (status == _SKIPPED and skip_origin in _READABLE_SKIPS):\n",
                        "    if (status not in _FAILED_FINALLY and status != _SKIPPED) or skip_origin in _READABLE_SKIPS:\n"),),
          f"{ROWS}::test_a_task_beside_an_upstream_that_still_runs_never_reads_its_older_output"),
    # The catcher parses the cause again: a cut cause fails it, and the task skips its retries.
    Probe("R-A1", (Edit(RUN_TASK, "$m := $match($causeStr, /\\\"ExecutionArn\\\"\\\\s*:\\\\s*\\\"([^\\\"]+)\\\"/, 1); "
                                  "$count($m) > 0 ? $m.groups[0] : '')",
                        "$parsed := $parse($causeStr); $exists($parsed.ExecutionArn) ? $parsed.ExecutionArn : '')"),),
          f"{ROWS}::test_a_failure_whose_cause_was_cut_still_goes_through_the_tasks_retries"),
    # The success neither retries an unnamed error nor reads its row after one: it waits for a decision.
    Probe("R-R1", (Edit(RUN_TASK, '            "States.ALL"\n          ],\n          "IntervalSeconds": 2,\n'
                                  '          "MaxAttempts": 3,',
                        '            "States.ALL"\n          ],\n          "IntervalSeconds": 2,\n'
                        '          "MaxAttempts": 0,'),
                   Edit(RUN_TASK, '"Next": "Read_After_Cancel",\n          "Comment": "Any other error, whose write',
                        '"Next": "Success_Not_Recorded",\n          "Comment": "Any other error, whose write')),
          f"{ROWS}::test_a_success_whose_write_failed_with_an_error_nobody_named_is_recorded"),
    # The date variables read the run's key as a day again: an hourly key fails $toMillis.
    Probe("R-G1", (Edit(RUN_TASK, "$dt := $toMillis($pd & 'T00:00:00Z')", "$dt := $toMillis($cd & 'T00:00:00Z')"),),
          f"{ROWS}::test_a_run_of_an_hourly_weekly_or_monthly_partition_runs_its_tasks[hourly]"),
    # The success transaction writes the row whatever owns it: a superseded run_task records its late success.
    Probe("R-L2", (Edit(RUN_TASK, "'ConditionExpression': '#s = :running AND #g = :g'",
                        "'ConditionExpression': 'attribute_exists(execution_name)'"),),
          f"{ROWS}::test_a_late_success_of_a_run_task_the_engine_stopped_records_nothing"),
    # A timeout skips the task's retries, as execution_timeout did in 1.2 (the review of ADR #136, B4).
    Probe("R-T2", (Edit(RUN_TASK, '"Condition": "{% $retry_attempt < (',
                        '"Condition": "{% $states.input.error_obj.Error != \'States.Timeout\' and $retry_attempt < ('),),
          f"{ROWS}::test_a_timed_out_attempt_goes_through_the_tasks_retries"),
    # A cancelled success transaction is not sent again: the success is lost, the task waits for a decision.
    Probe("R-C1", (Edit(RUN_TASK, '"Next": "Wait_Before_Recording_Again"', '"Next": "Success_Not_Recorded"'),),
          f"{ROWS}::test_a_cancelled_success_is_tried_again_and_recorded_with_its_fact"),
    # A success recorded without its facts: no consumer would ever see the partition (invariant 12).
    Probe("B-F3", (Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
                        "{'TransactItems': $append([$row, $output], $facts)}", "{'TransactItems': [$row, $output]}"),),
          f"{SCENARIOS}::test_a_success_whose_fact_cannot_be_recorded_waits_for_a_decision"),
    Probe("B-U4", (Edit("sam/lambdas/coordination/asset_fanout.py",
                        "known_facts=[evidence], recorded_by=evidence.event_time)", "known_facts=[evidence])"),),
          f"{SCENARIOS}::test_the_catch_up_starts_no_run_that_a_cascade_none_backfill_completed"),
    Probe("B-S2", (Edit("sam/lambdas/console_api/routes/executions.py",
                        "sfn_arn = (pipelines_repo.get(pipeline_name) or {}).get('sfn_arn')",
                        "sfn_arn = pipelines_repo.get_sfn_arn(pipeline_name)"),),
          f"{SCENARIOS}::test_stopping_a_run_ends_its_execution"),
    Probe("B-R3", (Edit("sam/sfn_templates/helpers/restart_task/sfn.tpl.json",
                        '"IntervalSeconds": 5,\n          "MaxAttempts": 2,\n          "BackoffRate": 2,\n          "JitterStrategy": "FULL"\n        }\n      ],\n      "Catch": [\n        {\n          "ErrorEquals": [\n            "States.ALL"\n          ],\n          "Comment": "The restart did not happen: say so in the bell, then fail.",\n          "Output": "{% $states.input ~> |$| {\'restart_error\': $states.errorOutput} | %}",\n          "Next": "Warn_Restart_Failed"\n        }\n      ]',
                        '"IntervalSeconds": 5,\n          "MaxAttempts": 2,\n          "BackoffRate": 2,\n          "JitterStrategy": "FULL"\n        }\n      ]'),),
          f"{SCENARIOS}::test_a_restart_that_cannot_start_its_wrapper_says_so_and_can_be_repeated"),
    Probe("B-M3", (Edit("sam/lambdas/console_api/routes/tasks.py",
                        "asset_events_repo, cascade=_row_cascade(item),", "asset_events_repo, cascade='auto',"),),
          f"{SCENARIOS}::test_mark_successful_keeps_a_cascade_none_backfill_from_starting_consumers"),
    # The old restart rebuilt the wrapper's input from a few saved fields, without wait_for. The
    # fallback for a pre-1.2 row now restores wait_for from the row, so both are put back.
    Probe("B-R1", (Edit("sam/sfn_templates/helpers/restart_task/sfn.tpl.json",
                        "$start := ($exists($item.starter_input.S) and $item.starter_input.S != '') ? $parse($item.starter_input.S) :",
                        "$start := false ? $parse($item.starter_input.S) :"),
                   Edit("sam/sfn_templates/helpers/restart_task/sfn.tpl.json",
                        "'wait_for': ($exists($states.input.item.wait_for.S) and $states.input.item.wait_for.S != '') ? $parse($states.input.item.wait_for.S) : [], ",
                        "")),
          f"{SCENARIOS}::test_a_restarted_task_is_still_gated_on_its_assets"),
    # ADR #137's tasks as rows (test_scenarios_rows.py).
    # A pass that runs out of budget no longer sets left_at, so no record wakes the next pass.
    Probe("R-S1", (Edit(DISPATCHER, "left=bool(state.changed) or state.run.left_at is None), quiet=True)",
                        "left=False), quiet=True)"),),
          f"{ROWS}::test_a_pass_out_of_budget_leaves_the_rest_to_the_next_pass_at_once"),
    # Two guards keep two passes at once from starting a task twice: each decision's write is conditioned
    # on the status it read, and run_task's name is derived from the row. The probe removes both.
    Probe("R-D1", (Edit("polyris/coordination/dispatch_writes.py",
                        'ROW_CONDITION = f"#status = :read AND {task_row.FENCE}"',
                        'ROW_CONDITION = f"(#status = :read OR attribute_exists(#status)) AND {task_row.FENCE}"'),
                   Edit("polyris/coordination/run_name.py",
                        "    digest = _digest(key, run_start, str(int(generation)), length=RUN_TASK_HASH)",
                        "    digest = _digest(key, run_start, str(int(generation)), str(__import__('time').time_ns()), "
                        "length=RUN_TASK_HASH)")),
          f"{ROWS}::test_two_passes_at_once_start_each_task_once"),
    # Two guards keep a late Start_Run attempt from reviving a run the engine stopped: the write of its token
    # and its activation are each conditioned on no stop mark. The probe removes both.
    Probe("R-S2", (Edit("polyris/coordination/run_row.py",
                        'TOKEN_CONDITION = "attribute_not_exists(#stop) AND (attribute_not_exists(#order) OR '
                        '#order <= :order)"',
                        'TOKEN_CONDITION = "(attribute_not_exists(#stop) OR attribute_exists(#stop)) AND '
                        '(attribute_not_exists(#order) OR #order <= :order)"'),
                   Edit("polyris/coordination/start_run.py",
                        '"#status = :creating AND attribute_not_exists(#stop) AND run_execution = :execution"',
                        '"#status = :creating AND (attribute_not_exists(#stop) OR attribute_exists(#stop)) '
                        'AND run_execution = :execution"')),
          f"{ROWS}::test_a_late_start_run_attempt_cannot_revive_a_run_the_engine_stopped"),
    # An access denied counts toward no limit: it waits for the permission. The probe counts it as throttling.
    Probe("R-S3", (Edit("polyris/coordination/dispatch.py", "    if kind == DENIED:\n",
                        "    if kind == DENIED and False:\n"),),
          f"{ROWS}::test_a_denied_start_warns_at_once_and_counts_toward_no_limit"),
    # A waiting row whose upstream's row is gone fails with the cause; the probe leaves it waiting for good.
    Probe("R-T1", (Edit("polyris/coordination/dispatch.py",
                        '        cause = f"the row of its upstream {missing[0]} is missing"\n'
                        '        return _final(row, "fail_wait", now, cause=cause, warning=f"{row.task_id}: {cause}")\n',
                        "        return Judgment(row.task_id, due=row.due)\n"),),
          f"{ROWS}::test_a_waiting_dependant_of_a_missing_upstream_row_fails_with_the_cause"),
)
