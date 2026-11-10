"""Tests for notify_asset_consumers_for_manual_success in task_actions.py.

Verifies the Mark Successful side-effect chain: record asset events, start the
push-consumer SFN, AND invoke NotifyAssetSubscribersFunction for pull-waiters
(the latter closes the gap where `mark_success` on an asset-producing task
would wake push consumers but leave `wait_for` subscribers hanging).
"""
import json

import pytest

from task_actions import notify_asset_consumers_for_manual_success


_OUTLETS = [
    {"name": "orders/clean", "uri": "s3://bucket/clean/"},
    {"name": "orders/analytics"},
]


@pytest.fixture
def events_repo(mocker):
    return mocker.MagicMock()


@pytest.fixture
def mock_sfn(mocker):
    return mocker.patch("task_actions.sfn")


@pytest.fixture
def mock_lambda(mocker):
    return mocker.patch("task_actions.lambda_client")


def test_wakes_wait_for_subscribers_via_lambda_invoke(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Mark Successful must invoke NotifyAssetSubscribersFunction so pull-
    waiters (`wait_for=[asset]`) are woken the same way natural completion
    wakes them. Fire-and-forget (InvocationType=Event)."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch(
        "task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN",
        "arn:aws:lambda:...:function:notify-asset-subscribers",
    )

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="extract",
        pipeline_name="orders-clean",
        date="2026-11-10",
        events_repo=events_repo,
    )

    mock_lambda.invoke.assert_called_once()
    call = mock_lambda.invoke.call_args
    assert call.kwargs["InvocationType"] == "Event"
    assert call.kwargs["FunctionName"] == "arn:aws:lambda:...:function:notify-asset-subscribers"
    payload = json.loads(call.kwargs["Payload"].decode("utf-8"))
    assert payload["outlets"] == _OUTLETS
    assert payload["source_task"] == "extract"
    assert payload["source_dag"] == "orders-clean"
    assert "event_time" in payload


def test_single_lambda_invoke_regardless_of_outlet_count(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """The Lambda invoke is one call per Mark Successful — the Lambda's own
    DAL handles per-outlet subscription lookup. Prevents N-outlet × M-invoke
    fan-out at the Console API."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch(
        "task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN",
        "arn:fn:notify",
    )

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,  # 2 outlets
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert mock_lambda.invoke.call_count == 1


def test_skips_lambda_invoke_when_env_unconfigured(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """OSS-only deploys without the pull-waiter Lambda wired should log a
    warning and skip the invoke — not crash."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "")

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    mock_lambda.invoke.assert_not_called()


def test_lambda_invoke_error_logged_but_does_not_raise(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Pull-waiter wake is best-effort — matches the push SFN start pattern
    above it. An InvokeFunction error must not fail the manual action."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "arn:fn:notify")
    mock_lambda.invoke.side_effect = RuntimeError("Lambda unreachable")

    # Must not raise
    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    mock_lambda.invoke.assert_called_once()


def test_empty_outlets_skips_everything(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    notify_asset_consumers_for_manual_success(
        outlets=[],
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    events_repo.put.assert_not_called()
    mock_sfn.start_execution.assert_not_called()
    mock_lambda.invoke.assert_not_called()
