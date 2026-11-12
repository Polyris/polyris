"""Tests for notify_asset_consumers_for_manual_success in task_actions.py.

Mark Successful records each outlet's fact for the marked run's partition
(ADR #130 §1, B-M1) and starts the push consumers. Tasks waiting on the
asset are woken by the asset-events stream, not by this function.
"""
import json

import pytest

import task_actions
from task_actions import notify_asset_consumers_for_manual_success

_OUTLETS = [
    {"name": "orders/clean", "uri": "s3://bucket/clean/", "granularity": "weekly"},
    {"name": "orders/analytics"},
]


@pytest.fixture
def events_repo(mocker):
    return mocker.MagicMock()


@pytest.fixture
def mock_sfn(mocker):
    return mocker.patch("task_actions.sfn")


def _call(events_repo, outlets=_OUTLETS, **kwargs):
    notify_asset_consumers_for_manual_success(
        outlets=outlets, task_name="extract", pipeline_name="orders-clean",
        partition_key="2026-11-10", events_repo=events_repo, **kwargs,
    )


def test_records_one_fact_per_outlet_for_the_marked_partition(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    _call(events_repo)
    calls = [c.kwargs for c in events_repo.write_fact.call_args_list]
    assert calls == [
        {"asset_name": "orders/clean", "partition_key": "2026-11-10", "granularity": "weekly", "cascade": "auto",
         "source_dag": "orders-clean", "source_task": "extract", "uri": "s3://bucket/clean/",
         "ttl_days": task_actions.ASSET_EVENT_TTL_DAYS},
        {"asset_name": "orders/analytics", "partition_key": "2026-11-10", "granularity": None, "cascade": "auto",
         "source_dag": "orders-clean", "source_task": "extract", "uri": "",
         "ttl_days": task_actions.ASSET_EVENT_TTL_DAYS},
    ]


def test_facts_live_as_long_as_run_task_facts():
    assert task_actions.ASSET_EVENT_TTL_DAYS == 365


def test_starts_push_consumers_per_outlet_for_the_marked_partition(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    _call(events_repo)
    assert mock_sfn.start_execution.call_count == 2
    first = json.loads(mock_sfn.start_execution.call_args_list[0].kwargs["input"])
    assert first == {"asset_name": "orders/clean", "execution_date": "2026-11-10", "source_task": "extract",
                     "source_dag": "orders-clean", "cascade_all": False}


def test_wakes_no_waiting_task_itself(mocker, events_repo, mock_sfn):
    """The stream wakes them; there is no Lambda to invoke any more."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    assert not hasattr(task_actions, "lambda_client")
    assert not hasattr(task_actions, "NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN")
    _call(events_repo)


def test_without_the_push_target_facts_are_still_recorded(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "")
    warn = mocker.patch.object(task_actions.log, "warn")
    _call(events_repo)
    assert events_repo.write_fact.call_count == 2
    mock_sfn.start_execution.assert_not_called()
    warn.assert_called_once()


def test_a_failed_fact_is_logged_and_the_rest_continue(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    events_repo.write_fact.side_effect = [RuntimeError("AccessDenied"), None]
    error = mocker.patch.object(task_actions.log, "error")
    _call(events_repo)
    assert events_repo.write_fact.call_count == 2
    assert mock_sfn.start_execution.call_count == 2
    assert error.call_args.kwargs["error"] == "AccessDenied"


def test_a_failed_push_start_is_logged_and_does_not_raise(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mock_sfn.start_execution.side_effect = RuntimeError("Throttled")
    error = mocker.patch.object(task_actions.log, "error")
    _call(events_repo)
    assert error.call_count == 2


def test_outlets_without_a_name_are_ignored(mocker, events_repo, mock_sfn):
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    _call(events_repo, outlets=[{"uri": "s3://x"}])
    events_repo.write_fact.assert_not_called()


def test_empty_outlets_skip_everything(events_repo, mock_sfn):
    _call(events_repo, outlets=[])
    events_repo.write_fact.assert_not_called()
    mock_sfn.start_execution.assert_not_called()
