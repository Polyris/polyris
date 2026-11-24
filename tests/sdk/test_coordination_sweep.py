"""The sweeper's pure rules (ADR #130 §8, §9; ADR #137 §6, §7)."""

from datetime import datetime, timedelta, timezone

import pytest

from polyris.coordination import sweep
from polyris.coordination.times import format_utc

STARTED = datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)


def test_push_retries_back_off_from_5_minutes_to_an_hour():
    delays = [sweep.next_push_retry(STARTED, n) - STARTED for n in range(6)]
    assert delays == [timedelta(minutes=m) for m in (5, 10, 20, 40, 60, 60)]


def test_the_catch_up_runs_hourly_and_at_once_when_it_never_ran():
    assert sweep.catch_up_due(STARTED, None)
    assert sweep.catch_up_due(STARTED, STARTED - timedelta(hours=1))
    assert not sweep.catch_up_due(STARTED, STARTED - timedelta(minutes=59))


# ── engine findings (ADR #130 §9): what the console's System status shows ──

#: The names the sweeper writes into its report, as a deployed stack has them.
NAMES = {"sweeper_function": "acme-dev-polyris-sweeper", "fanout_function": "acme-dev-polyris-asset-fanout",
         "events_table": "acme-dev-polyris-asset-events",
         "fanout_mapping_uuid": "48529b7b-abda-4306-8550-8d3bbc1391c3", "region": "eu-west-1",
         "dispatcher_function": "acme-dev-polyris-reconcile", "tasks_table": "acme-dev-polyris-pipeline-tokens",
         "dispatch_mapping_uuid": "7d0c1f52-2e5a-4bb1-9d5e-0b9f0e3c6a10"}
SWEEPER_LOGS = "aws logs tail /aws/lambda/acme-dev-polyris-sweeper --region eu-west-1 --since 1h"
FANOUT_LOGS = "aws logs tail /aws/lambda/acme-dev-polyris-asset-fanout --region eu-west-1 --since 1h"
DISPATCHER_LOGS = "aws logs tail /aws/lambda/acme-dev-polyris-reconcile --region eu-west-1 --since 1h"
LATE = ("Tasks that wait for an asset start up to 40 minutes late, and pipelines scheduled on an asset "
        "up to an hour late.")
NOT_STARTING = ("Tasks that wait for an asset, and pipelines scheduled on an asset, do not start while the "
                "periodic re-check is also not running.")
SILENT = "2026-11-12T04:40:00.000Z"          # 20 minutes before STARTED: past STALE_AFTER
BEHIND = "2026-11-12T03:00:00.000Z"          # 2 hours before STARTED: past BEHIND_AFTER


def _report(**fields):
    """A fresh report of a healthy engine, as the sweeper writes it, with `fields` changed."""
    base = {"checked_at": "2026-11-12T04:58:00.000Z", "fanout_mapping": "Enabled", "fanout_result": "OK",
            "stream": "ENABLED", "dispatch_mapping": "Enabled", "dispatch_result": "OK", "tasks_stream": "ENABLED",
            "lost_hint_runs": 0, "oldest_overdue": "", "failed_steps": [], **NAMES}
    return {**base, **fields}


def _without(report, *fields):
    return {k: v for k, v in report.items() if k not in fields}


@pytest.mark.parametrize("delta,said", [
    (timedelta(minutes=15, seconds=59), "15 min ago"),
    (timedelta(minutes=119, seconds=59), "119 min ago"),
    (timedelta(minutes=120), "2 h ago"),
    (timedelta(hours=47, minutes=59), "47 h ago"),
    (timedelta(hours=48), "2 days ago"),
    (timedelta(days=9, hours=23), "9 days ago"),
])
def test_ago_says_minutes_then_hours_then_days_each_floored(delta, said):
    assert sweep.ago(delta) == said


def test_a_fresh_report_with_everything_enabled_has_no_findings():
    assert sweep.engine_findings(_report(), STARTED) == []
    for result in ("OK", "No records processed", ""):
        assert sweep.engine_findings(_report(fanout_result=result), STARTED) == []
    # The limits themselves are not past.
    assert sweep.engine_findings(_report(checked_at=format_utc(STARTED - sweep.STALE_AFTER)), STARTED) == []
    assert sweep.engine_findings(_report(oldest_overdue=format_utc(STARTED - sweep.BEHIND_AFTER)), STARTED) == []


def test_1_without_a_report_only_no_check_yet_applies():
    expected = [{
        "title": "No check has run yet",
        "impact": "Waiting tasks are not re-checked until the periodic re-check runs. "
                  "It first runs within 5 minutes of an install or upgrade.",
        "fix": "If this stays for more than 10 minutes, check in EventBridge Scheduler that the schedule "
               "whose name ends in -polyris-sweeper is enabled.",
    }]
    assert sweep.engine_findings(None, STARTED) == expected
    assert sweep.engine_findings({}, STARTED) == expected
    # Whatever else a report without a check time says.
    assert sweep.engine_findings(_report(checked_at="", fanout_mapping="Disabled", stream="DISABLED",
                                         failed_steps=["catch_up: ClientError: x"]), STARTED) == expected


def test_2_a_silent_re_check_names_its_last_run_and_both_commands():
    assert sweep.engine_findings(_report(checked_at=SILENT), STARTED) == [{
        "title": "The periodic re-check is not running",
        "resource": "The last run of acme-dev-polyris-sweeper was 20 min ago. It runs every 5 minutes.",
        "impact": "Tasks still start from the streams. A task whose wake-up was lost waits, and no limit, countdown "
                  "or decision timeout ends, until the check runs again.",
        "fix": "Check that its schedule in EventBridge Scheduler is enabled. If it is, read the function's logs.",
        "command": "aws scheduler get-schedule --name acme-dev-polyris-sweeper --region eu-west-1 --query State\n"
                   + SWEEPER_LOGS,
    }]


def test_3_a_mapping_that_is_off_names_its_state_when_it_changed_and_the_command():
    report = _report(fanout_mapping="Disabled", fanout_mapping_changed_at="2026-11-12T04:22:00.000Z")
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Asset event delivery is off",
        "resource": "The stream mapping of acme-dev-polyris-asset-fanout is Disabled.",
        "changed_at": "2026-11-12T04:22:00.000Z",
        "impact": LATE,
        "fix": "Re-enable the mapping: in the Lambda console, open acme-dev-polyris-asset-fanout, "
               "then Configuration → Triggers.",
        "command": "aws lambda update-event-source-mapping --uuid 48529b7b-abda-4306-8550-8d3bbc1391c3 --enabled "
                   "--region eu-west-1",
    }]


@pytest.mark.parametrize("state", ["Disabling", "Enabling", "Updating", "unknown"])
def test_3_any_mapping_state_but_enabled_missing_or_unknown_with_a_detail_is_off(state):
    [finding] = sweep.engine_findings(_report(fanout_mapping=state), STARTED)
    assert finding["title"] == "Asset event delivery is off"
    assert finding["resource"] == f"The stream mapping of acme-dev-polyris-asset-fanout is {state}."
    assert "changed_at" not in finding                      # a report without the time AWS changed it
    assert "command" not in finding                         # AWS enables only a mapping that is Disabled


@pytest.mark.parametrize("state", sorted(sweep.MAPPING_SETTLING))
def test_3_a_mapping_a_deploy_is_changing_is_not_off_until_it_stays_so(state):
    """Creating, Enabling and Updating last seconds during a deploy; past STALE_AFTER they are stuck."""
    recent = _report(fanout_mapping=state, fanout_mapping_changed_at="2026-11-12T04:50:00.000Z")   # 10 min before
    stuck = _report(fanout_mapping=state, fanout_mapping_changed_at=SILENT)                         # 20 min before
    assert sweep.engine_findings(recent, STARTED) == []
    assert [f["title"] for f in sweep.engine_findings(stuck, STARTED)] == ["Asset event delivery is off"]


@pytest.mark.parametrize("report", [
    _report(fanout_mapping=sweep.MAPPING_MISSING),
    _report(fanout_mapping=""),                             # an empty state reads as no mapping
    _without(_report(), "fanout_mapping"),
])
def test_3b_a_missing_mapping_is_not_set_up_and_has_no_command(report):
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Asset event delivery is not set up",
        "resource": "There is no stream mapping for acme-dev-polyris-asset-fanout.",
        "impact": LATE,
        "fix": "Recreate the trigger as the platform template defines it: in the Lambda console, open "
               "acme-dev-polyris-asset-fanout, then Add trigger → DynamoDB → the stream of acme-dev-polyris-asset-events, "
               "starting position Trim horizon, batch size 10, filter {\"eventName\": [\"INSERT\"]}. Deploying the same "
               "version again does not restore it.",
    }]


@pytest.mark.parametrize("mapping,title", [("Disabled", "Asset event delivery is off"),
                                           (sweep.MAPPING_MISSING, "Asset event delivery is not set up")])
def test_3_and_3b_with_the_re_check_silent_too_say_that_tasks_do_not_start(mapping, title):
    findings = sweep.engine_findings(_report(checked_at=SILENT, fanout_mapping=mapping), STARTED)
    assert [f["title"] for f in findings] == ["The periodic re-check is not running", title]
    assert findings[1]["impact"] == NOT_STARTING


def test_3c_a_mapping_that_could_not_be_read_does_not_claim_that_delivery_is_off():
    report = _report(fanout_mapping=sweep.unknown_state("AccessDeniedException"))
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Asset event delivery could not be checked",
        "resource": "Reading the stream mapping of acme-dev-polyris-asset-fanout failed (AccessDeniedException).",
        "impact": "If delivery is off, tasks that wait for an asset start late.",
        "fix": "Check that the sweeper's role may call lambda:ListEventSourceMappings, then read the re-check's logs.",
        "command": SWEEPER_LOGS,
    }]
    # Not even while the re-check is silent.
    findings = sweep.engine_findings({**report, "checked_at": SILENT}, STARTED)
    assert findings[1]["impact"] == "If delivery is off, tasks that wait for an asset start late."


def test_4_a_mapping_whose_last_call_failed_names_the_result_and_the_fanout_logs():
    assert sweep.engine_findings(_report(fanout_result="PROBLEM: Function call failed"), STARTED) == [{
        "title": "Asset event delivery is failing",
        "resource": "The last call of the stream mapping of acme-dev-polyris-asset-fanout failed: "
                    "PROBLEM: Function call failed.",
        "impact": "Tasks that wait for an asset start up to 40 minutes late. The failed records are retried "
                  "until they succeed or expire after 24 hours.",
        "fix": "Read the logs of acme-dev-polyris-asset-fanout: the error there names the cause.",
        "command": FANOUT_LOGS,
    }]


@pytest.mark.parametrize("stream,shown", [("DISABLED", "DISABLED"), ("", "missing")])
def test_5_a_stream_that_is_off_is_named_without_a_command(stream, shown):
    assert sweep.engine_findings(_report(stream=stream), STARTED) == [{
        "title": "Asset events are not recorded for delivery",
        "resource": f"The stream of acme-dev-polyris-asset-events is {shown}.",
        "impact": "New facts wake no waiting task. The periodic re-check still starts them, up to 40 minutes late.",
        "fix": "Turn the stream on again (view type NEW_IMAGE). That makes a new stream: add a trigger on it to "
               "acme-dev-polyris-asset-fanout, then delete the trigger on the old one.",
    }]


def test_5c_a_stream_that_could_not_be_read_is_named_as_not_checked():
    report = _report(stream=sweep.unknown_state("ResourceNotFoundException"))
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Asset event recording could not be checked",
        "resource": "Reading the stream of acme-dev-polyris-asset-events failed (ResourceNotFoundException).",
        "impact": "If the stream is off, tasks that wait for an asset start late.",
        "fix": "Check that the sweeper's role may call dynamodb:DescribeTable, then read the re-check's logs.",
        "command": SWEEPER_LOGS,
    }]


def test_6_waiting_tasks_behind_their_checks_name_how_long_ago_the_oldest_was_due():
    assert sweep.engine_findings(_report(oldest_overdue=BEHIND), STARTED) == [{
        "title": "Waiting tasks are behind their checks",
        "resource": "The oldest check was due 2 h ago.",
        "impact": "Tasks that could run start late.",
        "fix": "Read the re-check's logs: it cannot keep up, or it fails on some rows.",
        "command": SWEEPER_LOGS,
    }]


def test_7_each_failed_step_is_one_finding_named_in_words():
    failed = ["catch_up: ClientError: throttled",
              "self_check: the push retry backlog could not be read: ClientError: x",
              "a_later_step: KeyError: 'k'"]
    findings = sweep.engine_findings(_report(failed_steps=failed), STARTED)
    assert [(f["title"], f["resource"]) for f in findings] == [
        ("The periodic re-check could not finish the hourly push catch-up", "ClientError: throttled"),
        ("The periodic re-check could not finish its self-check",
         "the push retry backlog could not be read: ClientError: x"),
        ("The periodic re-check could not finish a_later_step", "KeyError: 'k'"),   # a step it has no words for
    ]
    assert all(f["impact"] == "The other steps still ran. The next run, within 5 minutes, tries again."
               and f["fix"] == "If this stays, the error names the cause; the re-check's logs have the full message."
               and f["command"] == SWEEPER_LOGS for f in findings)


def test_findings_come_in_the_order_of_the_clauses():
    report = _report(checked_at=SILENT, fanout_mapping="Disabled", fanout_result="PROBLEM: x", stream="DISABLED",
                     dispatch_mapping="Disabled", dispatch_result="PROBLEM: y", tasks_stream="DISABLED",
                     oldest_overdue=BEHIND, lost_hint_runs=3, failed_steps=["due_runs: 3 of 4 passes over runs failed"])
    assert [f["title"] for f in sweep.engine_findings(report, STARTED)] == [
        "The periodic re-check is not running",
        "Asset event delivery is off",
        "Asset event delivery is failing",
        "Asset events are not recorded for delivery",
        "Task delivery is off",
        "Task delivery is failing",
        "Task changes are not recorded for delivery",
        "Waiting tasks are behind their checks",
        "Runs missed their wake-ups",
        "The periodic re-check could not finish checking the runs that are due",
    ]


def test_without_the_sweeper_name_it_reads_the_sweeper_function():
    [finding] = sweep.engine_findings(_without(_report(checked_at=SILENT), "sweeper_function"), STARTED)
    assert finding["resource"] == "The last run of the sweeper function was 20 min ago. It runs every 5 minutes."


@pytest.mark.parametrize("fields,field,text", [
    ({"fanout_mapping": "Disabled"}, "resource", "The stream mapping of the asset-fanout function is Disabled."),
    ({"fanout_mapping": "Disabled"}, "fix",
     "Re-enable the mapping: in the Lambda console, open the asset-fanout function, then Configuration → Triggers."),
    ({"fanout_mapping": "missing"}, "resource", "There is no stream mapping for the asset-fanout function."),
    ({"fanout_mapping": "unknown (Throttling)"}, "resource",
     "Reading the stream mapping of the asset-fanout function failed (Throttling)."),
    ({"fanout_result": "PROBLEM: x"}, "resource", "The last call of the stream mapping of the asset-fanout function "
                                                  "failed: PROBLEM: x."),
    ({"fanout_result": "PROBLEM: x"}, "fix", "Read the logs of the asset-fanout function: the error there names "
                                             "the cause."),
    ({"stream": "DISABLED"}, "fix", "Turn the stream on again (view type NEW_IMAGE). That makes a new stream: add a "
                                    "trigger on it to the asset-fanout function, then delete the trigger on the old one."),
])
def test_without_the_fanout_name_it_reads_the_asset_fanout_function(fields, field, text):
    [finding] = sweep.engine_findings(_without(_report(**fields), "fanout_function"), STARTED)
    assert finding[field] == text


@pytest.mark.parametrize("stream,text", [
    ("DISABLED", "The stream of the asset-events table is DISABLED."),
    ("unknown (AccessDeniedException)", "Reading the stream of the asset-events table failed (AccessDeniedException)."),
])
def test_without_the_table_name_it_reads_the_asset_events_table(stream, text):
    [finding] = sweep.engine_findings(_without(_report(stream=stream), "events_table"), STARTED)
    assert finding["resource"] == text


#: A report with one finding that has a command, and the report fields that command names.
COMMANDS = [
    ({"checked_at": SILENT}, {"sweeper_function", "region"}),
    ({"fanout_mapping": "Disabled"}, {"fanout_mapping_uuid", "region"}),
    ({"fanout_mapping": "unknown (Throttling)"}, {"sweeper_function", "region"}),
    ({"fanout_result": "PROBLEM: x"}, {"fanout_function", "region"}),
    ({"stream": "unknown (Throttling)"}, {"sweeper_function", "region"}),
    ({"oldest_overdue": BEHIND}, {"sweeper_function", "region"}),
    ({"failed_steps": ["due_runs: x"]}, {"sweeper_function", "region"}),
    ({"dispatch_mapping": "Disabled"}, {"dispatch_mapping_uuid", "region"}),
    ({"dispatch_mapping": "unknown (Throttling)"}, {"sweeper_function", "region"}),
    ({"dispatch_result": "PROBLEM: x"}, {"dispatcher_function", "region"}),
    ({"tasks_stream": "unknown (Throttling)"}, {"sweeper_function", "region"}),
    ({"lost_hint_runs": 2}, {"dispatcher_function", "region"}),
]


@pytest.mark.parametrize("fields,names", COMMANDS)
def test_a_command_is_there_only_when_every_name_it_uses_is_known(fields, names):
    [finding] = sweep.engine_findings(_report(**fields), STARTED)
    assert all(NAMES[name] in finding["command"] for name in names)
    for name in NAMES:
        for unknown in (_without(_report(**fields), name), _report(**fields, **{name: ""})):
            [shown] = sweep.engine_findings(unknown, STARTED)
            assert ("command" in shown) is (name not in names), name
            if name not in names:
                assert shown["command"] == finding["command"]


def test_the_lease_outlives_a_run_but_not_the_schedule():
    assert sweep.BUDGET < sweep.LEASE < timedelta(minutes=5)


# ── ADR #137 §7: lost hints and a failed step ────────────────────────

@pytest.mark.parametrize("enabled_minutes_ago,left_at,countdown,lost", [
    (10, False, False, True),
    (4, False, False, False),          # its stream pass may still be under way
    (10, True, False, False),          # a pass left it for its budget
    (10, False, True, False),          # the sweeper is how a countdown ends
])
def test_a_lost_hint_is_an_old_enabling_time_that_no_pass_left(enabled_minutes_ago, left_at, countdown, lost):
    check = STARTED + timedelta(hours=1)
    enabled = check - timedelta(minutes=enabled_minutes_ago)
    assert sweep.lost_hint(enabled, check, left_at=left_at, countdown_ended=countdown) is lost
    assert sweep.lost_hint(None, check, left_at=False, countdown_ended=False) is False


def test_a_hint_lost_before_a_resume_counts_from_the_resume():
    """A row that a resume released is no lost hint while the resume's own pass may still run."""
    check = STARTED + timedelta(hours=1)
    enabled = check - timedelta(minutes=30)
    assert sweep.lost_hint(enabled, check, left_at=False, countdown_ended=False,
                           resumed_at=check - timedelta(minutes=2)) is False
    assert sweep.lost_hint(enabled, check, left_at=False, countdown_ended=False,
                           resumed_at=check - timedelta(minutes=20)) is True


@pytest.mark.parametrize("passes,failed,reported", [(10, 6, True), (10, 5, False), (3, 2, True), (1, 1, False),
                                                    (2, 2, True), (0, 0, False)])
def test_a_sweep_reports_a_failed_step_when_most_of_its_passes_failed_and_at_least_two(passes, failed, reported):
    assert sweep.failed_step(passes, failed) is reported


def test_the_failed_step_of_failed_passes_counts_them_and_names_the_first_cause():
    assert sweep.failed_passes(5, 3, "ValueError: bad spec") == (
        "3 of 5 passes over runs failed; the first: ValueError: bad spec")


def test_a_runs_lost_hint_warning_counts_its_tasks_and_names_a_few():
    assert sweep.lost_hints_warning(["load"]) == (
        "The periodic check found 1 task of this run that could have been decided earlier: load. Their wake-up "
        "hint was lost on the way, so they waited for this check.")
    many = [f"t{i}" for i in range(7, 0, -1)]
    assert sweep.lost_hints_warning(many).startswith(
        "The periodic check found 7 tasks of this run that could have been decided earlier: t1, t2, t3, t4, t5 "
        "and 2 more.")
    with pytest.raises(ValueError):
        sweep.lost_hints_warning([])


# ── ADR #137 §6: the tasks table's stream and its mapping, and the runs with lost hints ──

TASKS_LATE = ("Finished tasks start their dependants, and runs end, only at their run's periodic check, up to 30 "
              "minutes late.")


def test_8_a_missing_task_mapping_says_how_to_recreate_it():
    assert sweep.engine_findings(_report(dispatch_mapping=sweep.MAPPING_MISSING), STARTED) == [{
        "title": "Task delivery is not set up",
        "resource": "There is no stream mapping for acme-dev-polyris-reconcile.",
        "impact": TASKS_LATE,
        "fix": "Recreate the trigger as the platform template defines it (TasksDispatchMapping): in the Lambda "
               "console, open acme-dev-polyris-reconcile, then Add trigger → DynamoDB → the stream of "
               "acme-dev-polyris-pipeline-tokens, starting position Trim horizon, batch size 10, with the template's "
               "four filters. Deploying the same version again does not restore it.",
    }]
    # A report written before 1.3 has no reading of it, which reads as no mapping.
    [finding] = sweep.engine_findings(_without(_report(), "dispatch_mapping"), STARTED)
    assert finding["title"] == "Task delivery is not set up"


def test_8b_a_disabled_task_mapping_names_when_it_changed_and_how_to_enable_it():
    report = _report(dispatch_mapping="Disabled", dispatch_mapping_changed_at="2026-11-12T04:22:00.000Z")
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Task delivery is off",
        "resource": "The stream mapping of acme-dev-polyris-reconcile is Disabled.",
        "changed_at": "2026-11-12T04:22:00.000Z",
        "impact": TASKS_LATE,
        "fix": "Re-enable the mapping: in the Lambda console, open acme-dev-polyris-reconcile, then "
               "Configuration → Triggers.",
        "command": "aws lambda update-event-source-mapping --uuid 7d0c1f52-2e5a-4bb1-9d5e-0b9f0e3c6a10 --enabled "
                   "--region eu-west-1",
    }]


@pytest.mark.parametrize("state", sorted(sweep.MAPPING_SETTLING))
def test_8c_a_task_mapping_that_settles_after_a_deploy_is_no_finding_until_it_stays(state):
    recent = _report(dispatch_mapping=state, dispatch_mapping_changed_at="2026-11-12T04:50:00.000Z")
    stuck = _report(dispatch_mapping=state, dispatch_mapping_changed_at=SILENT)
    assert sweep.engine_findings(recent, STARTED) == []
    assert [f["title"] for f in sweep.engine_findings(stuck, STARTED)] == ["Task delivery is off"]


def test_8d_a_task_mapping_that_could_not_be_read_or_failed_its_last_call():
    unread = sweep.engine_findings(_report(dispatch_mapping=sweep.unknown_state("AccessDeniedException")), STARTED)
    assert unread == [{
        "title": "Task delivery could not be checked",
        "resource": "Reading the stream mapping of acme-dev-polyris-reconcile failed (AccessDeniedException).",
        "impact": "If delivery is off, tasks start late.",
        "fix": "Check that the sweeper's role may call lambda:ListEventSourceMappings, then read the re-check's logs.",
        "command": SWEEPER_LOGS,
    }]
    failing = sweep.engine_findings(_report(dispatch_result="PROBLEM: Function call failed"), STARTED)
    assert failing == [{
        "title": "Task delivery is failing",
        "resource": "The last call of the stream mapping of acme-dev-polyris-reconcile failed: "
                    "PROBLEM: Function call failed.",
        "impact": "Tasks of the runs in the failing records start late, at their run's periodic check. The failed "
                  "records are retried until they succeed or expire after 24 hours.",
        "fix": "Read the logs of acme-dev-polyris-reconcile: the error there names the cause.",
        "command": DISPATCHER_LOGS,
    }]


def test_8e_a_silent_re_check_says_that_nothing_decides_the_tasks_then():
    findings = sweep.engine_findings(_report(checked_at=SILENT, dispatch_mapping="Disabled"), STARTED)
    assert findings[1]["impact"] == ("Finished tasks do not start their dependants, and runs do not end, while the "
                                     "periodic check is also not running.")


@pytest.mark.parametrize("stream,shown", [("DISABLED", "DISABLED"), ("", "missing")])
def test_9_a_tasks_stream_that_is_off_is_named(stream, shown):
    assert sweep.engine_findings(_report(tasks_stream=stream), STARTED) == [{
        "title": "Task changes are not recorded for delivery",
        "resource": f"The stream of acme-dev-polyris-pipeline-tokens is {shown}.",
        "impact": "Finished tasks wake no dependant. The periodic check still decides them, up to 30 minutes late.",
        "fix": "Turn the stream on again (view type NEW_AND_OLD_IMAGES). That makes a new stream: add a trigger on "
               "it to acme-dev-polyris-reconcile as the template defines it, then delete the trigger on the old one.",
    }]


def test_9b_a_tasks_stream_that_could_not_be_read_is_named_as_not_checked():
    report = _report(tasks_stream=sweep.unknown_state("AccessDeniedException"))
    assert sweep.engine_findings(report, STARTED) == [{
        "title": "Task changes could not be checked",
        "resource": "Reading the stream of acme-dev-polyris-pipeline-tokens failed (AccessDeniedException).",
        "impact": "If the stream is off, tasks start late.",
        "fix": "Check that the sweeper's role may call dynamodb:DescribeTable, then read the re-check's logs.",
        "command": SWEEPER_LOGS,
    }]


@pytest.mark.parametrize("runs,said", [(1, "1 run"), (4, "4 runs")])
def test_10_runs_with_lost_hints_show_in_the_system_status(runs, said):
    """A shard held by a record that keeps failing shows here, though LastProcessingResult may read OK (TR-0)."""
    assert sweep.engine_findings(_report(lost_hint_runs=runs), STARTED) == [{
        "title": "Runs missed their wake-ups",
        "resource": f"The last check found {said} with tasks whose wake-up was lost.",
        "impact": "Those tasks started or finished at their run's periodic check, up to 30 minutes late. A record "
                  "that keeps failing in the tasks stream holds back the runs of its shard for up to 24 hours.",
        "fix": "Read the logs of acme-dev-polyris-reconcile, which the tasks stream calls: a record that keeps "
               "failing names its cause there. The bell names the runs.",
        "command": DISPATCHER_LOGS,
    }]


@pytest.mark.parametrize("fields,field,text", [
    ({"dispatch_mapping": "Disabled"}, "resource", "The stream mapping of the dispatcher function is Disabled."),
    ({"tasks_stream": "DISABLED"}, "resource", "The stream of the tasks table is DISABLED."),
    ({"lost_hint_runs": 1}, "fix", "Read the logs of the dispatcher function, which the tasks stream calls: a record "
                                   "that keeps failing names its cause there. The bell names the runs."),
])
def test_without_the_task_names_it_reads_them_in_words(fields, field, text):
    report = _without(_report(**fields), "dispatcher_function", "tasks_table")
    [finding] = sweep.engine_findings(report, STARTED)
    assert finding[field] == text


def test_a_sweeper_run_makes_no_more_calls_than_it_was_given():
    """A sweeper run's DescribeExecution calls stay below Step Functions' bucket of 300 (the TR-7 estimate, S5)."""
    calls = sweep.Calls(2)
    assert [calls.take() for _ in range(3)] == [True, True, False]
    assert calls.left == 0
    assert 0 < sweep.DESCRIBES < 250              # the bucket outside us-east-1, us-west-2 and eu-west-1


def test_a_runs_rows_get_calls_the_longest_due_first_up_to_their_share(monkeypatch):
    """D141: one large run never takes every call of a sweeper run, and a row judged comes due behind those still
    waiting, so none waits for ever (TR-10: A-1, B-2, R10-1)."""
    monkeypatch.setattr(sweep, "DESCRIBES", 8)          # a share of 2 rows a run; 2 calls kept for the runs' checks
    at = datetime(2026, 11, 24, tzinfo=timezone.utc)
    due = [(at + timedelta(minutes=minutes), name) for minutes, name in ((30, "a"), (10, "z"), (20, "m"))]
    calls = sweep.Calls(8)
    assert sweep.grant(due, calls) == {"z", "m"} and calls.left == 6
    assert sweep.grant(due, sweep.Calls(3)) == {"z"}    # 2 calls stay for the runs' own executions
    assert sweep.grant(due, sweep.Calls(2)) == set()
    assert sweep.grant([], sweep.Calls(8)) == set()


def test_a_call_taken_for_a_check_that_is_not_due_goes_back():
    calls = sweep.Calls(1)
    assert calls.take() and calls.left == 0
    calls.give_back()
    assert calls.left == 1 and not calls.take(keep=1) and calls.take()
