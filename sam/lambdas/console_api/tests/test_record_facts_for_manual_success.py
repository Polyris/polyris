"""Tests for record_facts_for_manual_success in task_actions.py.

Mark Successful records each outlet's fact for the marked run's partition
(ADR #130 §1, B-M1). The asset-events stream wakes the waiting tasks and
starts the push consumers; this function starts nothing itself.
"""
import task_actions
from task_actions import record_facts_for_manual_success

_OUTLETS = [
    {"name": "orders/clean", "uri": "s3://bucket/clean/", "granularity": "weekly"},
    {"name": "orders/analytics"},
]


def _call(events_repo, outlets=_OUTLETS):
    return record_facts_for_manual_success(outlets=outlets, task_name="extract", pipeline_name="orders-clean",
                                           partition_key="2026-11-10", events_repo=events_repo, cascade="auto")


def test_records_one_fact_per_outlet_for_the_marked_partition(mocker):
    events_repo = mocker.MagicMock()
    _call(events_repo)
    assert [c.kwargs for c in events_repo.write_fact.call_args_list] == [
        {"asset_name": "orders/clean", "partition_key": "2026-11-10", "granularity": "weekly", "cascade": "auto",
         "source_dag": "orders-clean", "source_task": "extract", "uri": "s3://bucket/clean/",
         "ttl_days": task_actions.ASSET_EVENT_TTL_DAYS},
        {"asset_name": "orders/analytics", "partition_key": "2026-11-10", "granularity": None, "cascade": "auto",
         "source_dag": "orders-clean", "source_task": "extract", "uri": "",
         "ttl_days": task_actions.ASSET_EVENT_TTL_DAYS},
    ]


def test_facts_live_as_long_as_run_task_facts():
    assert task_actions.ASSET_EVENT_TTL_DAYS == 365


def test_starts_nothing_itself(mocker):
    """The stream wakes waiting tasks and starts push consumers; there is nothing to call."""
    sfn = mocker.patch("task_actions.sfn")
    _call(mocker.MagicMock())
    sfn.start_execution.assert_not_called()
    assert not hasattr(task_actions, "NOTIFY_ASSET_CONSUMERS_SFN_ARN")


def test_a_failed_fact_is_logged_returned_and_the_rest_continue(mocker):
    events_repo = mocker.MagicMock()
    events_repo.write_fact.side_effect = [RuntimeError("AccessDenied"), None]
    error = mocker.patch.object(task_actions.log, "error")
    failed = _call(events_repo)
    assert events_repo.write_fact.call_count == 2
    assert error.call_args.kwargs["error"] == "AccessDenied"
    assert failed == {_OUTLETS[0]["name"]: "AccessDenied"}


def test_outlets_without_a_name_or_no_outlets_record_nothing(mocker):
    events_repo = mocker.MagicMock()
    _call(events_repo, outlets=[{"uri": "s3://x"}])
    _call(events_repo, outlets=[])
    events_repo.write_fact.assert_not_called()
