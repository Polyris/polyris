"""Mutation probes: each re-introduces one defect in the new code (ADR #130, Testing).

A probe names a defect, the edits that bring it back (each an exact text
that must occur once in its file), and the scenario that must then fail.
Some defects are stopped by two guards; their probe removes both. The runner
(`python tests/backend/coordination/run_probes.py`) applies each probe, runs
its scenario, expects it to fail and restores the files. Probes target the
code the harness runs: the templates, the Lambdas and the coordination core
(polyris/coordination), which the Lambdas import. test_meta.py requires one for every defect whose
scenario runs.

An edit brings the defect back with a valid expression: a DynamoDB expression keeps every name and value
it binds, or the write is refused as invalid and the scenario meets an error path instead of the defect.
The first R-L2 probe dropped the names its condition used, and survived for that reason alone.
"""

from typing import NamedTuple, Tuple

SCENARIOS = "tests/backend/coordination/test_scenarios.py"
ROWS = "tests/backend/coordination/test_scenarios_rows.py"
DISPATCHER = "sam/lambdas/coordination/dispatcher.py"
CORE = "polyris/coordination/asset_conditions.py"
TASKS_ROUTE = "sam/lambdas/console_api/routes/tasks.py"
EXECUTIONS_ROUTE = "sam/lambdas/console_api/routes/executions.py"
RUN_TASK = "sam/sfn_templates/helpers/run_task/sfn.tpl.json"
ACTIONS = "polyris/coordination/actions.py"
DISPATCH = "polyris/coordination/dispatch.py"


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
    Probe("B-M1", (Edit(ACTIONS, "_facts(outlets, pipeline=pipeline, task_name=task_name, date=date,",
                        "_facts(outlets, pipeline=pipeline, task_name=task_name, date=format_utc(now)[:10],"),),
          f"{SCENARIOS}::test_mark_success_writes_the_fact_for_the_marked_runs_partition"),
    # A Resume that wakes no pass: tasks the pause held stay held until the run row's next check (ADR #137 §5).
    Probe("B-P1", (Edit("polyris/coordination/transitions.py",
                        "            or old.get(run_row.PAUSES) != new.get(run_row.PAUSES)\n", ""),),
          f"{SCENARIOS}::test_an_asset_arriving_during_a_pause_wakes_the_task_after_resume"),
    # The old registration replaced the row whatever it held.
    # run_task's success without the generation fence: the superseded work takes the row back from the Skip.
    Probe("B-O1", (Edit(RUN_TASK, "'ConditionExpression': '#s = :running AND #g = :g'",
                        "'ConditionExpression': '(#s = :running OR #s <> :running) AND (#g = :g OR #g <> :g)'"),),
          f"{SCENARIOS}::test_an_operator_skip_holds_when_the_run_task_could_not_be_stopped"),
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
    # A Restart that leaves the old final status: a dependant restarted beside it runs on it (B-R2).
    Probe("B-R2", (Edit("polyris/coordination/transitions.py",
                        '_task("restart", FINAL | {S.WAITING_DECISION.value}, S.WAITING.value,',
                        '_task("restart", FINAL | {S.WAITING_DECISION.value}, S.SUCCESS.value,'),),
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
    # run_task's running write without the generation fence takes the row back from an operator's decision.
    # A run_task whose generation an operator raised claims the row of the newer generation (Rule 47).
    # In 1.3 the decision also moves the status, so the claim must lose on both: an always-true condition that
    # keeps every name in use (an unused one fails the write with a ValidationException, probes.py header).
    Probe("B-O4", (Edit("sam/sfn_templates/helpers/run_task/sfn.tpl.json",
                        '"ConditionExpression": "#s = :deps_ready AND #g = :g",',
                        '"ConditionExpression": "(#s = :deps_ready OR #s <> :deps_ready) AND (#g = :g OR #g <> :g)",'),),
          f"{SCENARIOS}::test_run_task_never_starts_a_task_an_operator_decided_after_its_wait"),
    # The pause wait's heartbeat retry saved its token over an operator's decision, unconditionally.
    # An operator's Skip of a held row that does not take it out of the pause: the Resume releases it to run.
    Probe("B-O5", (Edit("polyris/coordination/transitions.py",
                        '_task("skip", _NO_OUTCOME_YET, S.SKIPPED.value,',
                        '_task("skip", _NO_OUTCOME_YET, S.WAITING_PAUSED.value,'),),
          f"{SCENARIOS}::test_a_task_an_operator_decided_while_paused_is_never_released_to_run"),
    # Mark Successful without its facts in its transaction: the tasks waiting on them hang (invariant 12).
    Probe("B-M2", (Edit(ACTIONS, '    if action == "mark_success":\n        items += [{"Put": fact}',
                        '    if False:\n        items += [{"Put": fact}'),),
          f"{SCENARIOS}::test_a_mark_success_whose_fact_cannot_be_recorded_records_nothing_and_a_restart_records_it"),
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
    # A console action that answers 500 after its row changed: a timeline write that fails escapes the route.
    Probe("B-RN4", (Edit(TASKS_ROUTE, "        except (ClientError, BotoCoreError) as e:\n            log.error(action, \"Could not "
                         "write the timeline event\"",
                         "        except KeyError as e:\n            log.error(action, \"Could not write the timeline event\""),),
          f"{SCENARIOS}::test_a_console_action_whose_timeline_cannot_be_written_still_applies"),
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
                                  "$count($m) > 0 ? $m[0].groups[0] : '')",
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
    # The mutated condition keeps every name and value, or DynamoDB refuses it as invalid and nothing is written.
    Probe("R-L2", (Edit(RUN_TASK, "'ConditionExpression': '#s = :running AND #g = :g'",
                        "'ConditionExpression': '(#s = :running OR #s <> :running) AND (#g = :g OR #g <> :g)'"),),
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
    Probe("B-S2", (Edit(EXECUTIONS_ROUTE, "            executions_repo.transact_write([{'Update': update}])",
                        "            executions_repo.transact_writes([{'Update': update}])"),),
          f"{SCENARIOS}::test_stopping_a_run_ends_its_execution"),
    Probe("B-M3", (Edit(TASKS_ROUTE, "outlets=outlets, cascade=run_input.from_item(stored).cascade if stored else 'auto',",
                        "outlets=outlets, cascade='auto',"),),
          f"{SCENARIOS}::test_mark_successful_keeps_a_cascade_none_backfill_from_starting_consumers"),
    # A Restart whose renewal writes no pointers: a fact recorded after it never wakes the restarted task.
    Probe("B-R1", (Edit(DISPATCHER, "                self._within_budget(state)\n                self._put_pointers(state, name)\n",
                        "                self._within_budget(state)\n"),),
          f"{SCENARIOS}::test_a_restarted_task_is_still_gated_on_its_assets"),
    Probe("R-S1", (Edit(DISPATCHER, "left=bool(state.changed) or state.run.left_at is None), quiet=True)",
                        "left=False), quiet=True)"),),
          f"{ROWS}::test_a_pass_out_of_budget_leaves_the_rest_to_the_next_pass_at_once"),
    # Two guards keep two passes at once from starting a task twice: each decision's write is conditioned
    # on the status it read, and run_task's name is derived from the row. The probe removes both.
    Probe("R-D1", (Edit("polyris/coordination/row_write.py",
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
    # A Restart with dependants that resets no decision of a rule: the dependant the failure decided stays
    # upstream_failed, and the restart of the dependants is lost.
    Probe("R-S4", (Edit(ACTIONS, "        decided = _RULE_DECISIONS.get((row.status, row.skip_origin))\n",
                        "        decided = None\n"),),
          f"{ROWS}::test_a_restart_with_dependants_inside_a_passs_read_reruns_the_dependant"),
    # The engine judges an answered run's execution: a Restart after the run's end is stopped with the run (D120).
    Probe("R-R2", (Edit(DISPATCH, "if run.stop is not None or run.answered or finished or run.started_at is None:",
                        "if run.stop is not None or finished or run.started_at is None:"),),
          f"{ROWS}::test_a_restart_after_the_runs_end_reruns_the_task_and_its_dependants"),
    # A pass that resets nothing leaves the request: it never clears. (Resetting decisions made since the request
    # too, the scenario's other way to fail, comes round in every pass and costs the harness 10,000 rounds.)
    Probe("R-R3", (Edit(DISPATCHER, "        if not names:\n            self._clear(state, run_row.RESET_DEPENDANTS)\n",
                        "        if not names:\n"),),
          f"{ROWS}::test_a_dependant_another_failure_still_blocks_is_reset_once_and_the_request_clears"),
    # The renewal leaves the outputs alone, as when only the Restart's own upstreams were renewed.
    Probe("R-O3", (Edit(ACTIONS, "for key in outputs if key in ttls and ttls[key] < year]",
                        "for key in outputs if key in ttls and ttls[key] < year and False]"),),
          f"{ROWS}::test_a_reset_dependant_finds_its_other_upstreams_output_long_after_a_late_restart"),
    # A Pause that raises no run_version: a pass that read the run row before it still writes deps_ready (§1, §5).
    Probe("R-P1", (Edit(ACTIONS, 'expression=f"SET #pauses = :pauses, {run_row.VERSION_RAISE}, #ttl = :ttl",',
                        'expression="SET #pauses = :pauses, #ttl = :ttl",'),),
          f"{ROWS}::test_a_pause_between_a_passs_read_and_its_write_starts_nothing"),
    # The paused time moves no limit: the hour's limit fires right after the Resume (§5).
    Probe("R-W1", (Edit("polyris/coordination/deadlines.py", "        if interval.end > begin:\n            deadline += "
                        "interval.end - begin\n", ""),),
          f"{ROWS}::test_a_limit_due_in_a_pause_fires_after_the_resume_moved_by_the_paused_time"),
    # A task whose facts are not ready starts anyway once its upstreams allow it (after a Resume, B-P2).
    Probe("B-P2", (Edit("polyris/coordination/decide.py", "        if not assets.ready:\n", "        if False:\n"),),
          f"{SCENARIOS}::test_resume_does_not_run_a_task_whose_assets_are_missing"),
    # Only a Stop stops a running task's run_task, as before the review of TR-6 (B-1).
    Probe("R-X1", (Edit(ACTIONS, "run_task_to_stop=run_task_name if row.status in _LIVE else None,",
                        'run_task_to_stop=run_task_name if action == "stop" and row.status in _LIVE else None,'),),
          f"{ROWS}::test_an_action_on_a_running_task_stops_its_work_and_a_restart_runs_one_copy"),
    # The answer's mark conditional on the run_version the pass read, as before D120.
    Probe("R-E2", (Edit("polyris/coordination/dispatch_writes.py",
                        'condition="#order = :order AND (attribute_not_exists(#answered) OR #answered <> :order)",',
                        'condition="#order = :order AND #rv = :rv_seen",'),
                   Edit("polyris/coordination/dispatch_writes.py",
                        'values={**run_row.VERSION_RAISE_VALUES, ":order": run.token_order, ":now": format_utc(now)})',
                        'values={**run_row.VERSION_RAISE_VALUES, ":order": run.token_order, ":now": format_utc(now), '
                        '":rv_seen": run.version})')),
          f"{ROWS}::test_a_restart_between_a_passs_read_of_every_row_final_and_its_answer_runs_after_the_runs_end"),
)
