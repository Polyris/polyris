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
    # B2: the wake-up invoke now goes through lambda_client_bounded (bounded
    # synchronous RequestResponse call). Tests that check invoke behaviour
    # must patch this client, not lambda_client.
    return mocker.patch("task_actions.lambda_client_bounded")


def test_wakes_wait_for_subscribers_via_lambda_invoke(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Mark Successful must invoke NotifyAssetSubscribersFunction so pull-
    waiters (`wait_for=[asset]`) are woken the same way natural completion
    wakes them. Bounded synchronous call (RequestResponse, ADR #128 §amended)
    so failures are detectable and can surface in the bell."""
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
    assert call.kwargs["InvocationType"] == "RequestResponse"
    assert call.kwargs["FunctionName"] == "arn:aws:lambda:...:function:notify-asset-subscribers"
    payload = json.loads(call.kwargs["Payload"].decode("utf-8"))
    assert payload["outlets"] == _OUTLETS
    assert payload["source_task"] == "extract"
    assert payload["source_dag"] == "orders-clean"
    assert "event_time" in payload


def test_records_asset_event_per_outlet_with_full_payload(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Pins the asset-events DDB put payload shape. If a field is renamed
    (source_dag → dag_name, asset_name → name, …), downstream SFN/query
    code breaks silently — mocks don't validate attribute names."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "fn:notify")

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="extract",
        pipeline_name="orders-clean",
        date="2026-11-10",
        events_repo=events_repo,
    )

    # One put per outlet (two outlets → two calls).
    assert events_repo.put.call_count == 2
    calls = events_repo.put.call_args_list
    first = calls[0].args[0]
    second = calls[1].args[0]
    # Pin every field; a rename would fail here.
    for payload, outlet in [(first, _OUTLETS[0]), (second, _OUTLETS[1])]:
        assert payload["asset_name"] == outlet["name"]
        assert payload["uri"] == outlet.get("uri", "")
        assert payload["source_task"] == "extract"
        assert payload["source_dag"] == "orders-clean"
        assert payload["execution_date"] == "2026-11-10"
        assert "event_time" in payload
        assert "ttl" in payload
        assert isinstance(payload["ttl"], int)


def test_starts_notify_asset_consumers_sfn_per_outlet(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Pins the push-consumer SFN start payload shape. Each outlet gets a
    start_execution call with cascade_all=False and the canonical input
    shape expected by notify_asset_consumers/sfn.tpl.json."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "arn:sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "fn:notify")

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="extract",
        pipeline_name="orders-clean",
        date="2026-11-10",
        events_repo=events_repo,
    )

    assert mock_sfn.start_execution.call_count == 2
    for call in mock_sfn.start_execution.call_args_list:
        assert call.kwargs["stateMachineArn"] == "arn:sfn:consumers"
        input_dict = json.loads(call.kwargs["input"])
        assert input_dict["source_task"] == "extract"
        assert input_dict["source_dag"] == "orders-clean"
        assert input_dict["execution_date"] == "2026-11-10"
        assert input_dict["cascade_all"] is False
        assert "asset_name" in input_dict


def test_single_warn_when_only_lambda_arn_missing(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """When only NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN is unset (SFN ARN is
    fine), one aggregated warn still fires listing just the missing one.
    Previously two separate warns fired on this path."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "arn:sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "")
    log_warn = mocker.patch("task_actions.log.warn")

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert log_warn.call_count == 1
    missing = log_warn.call_args.kwargs.get("missing", [])
    assert any("NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN" in m for m in missing)
    assert not any("NOTIFY_ASSET_CONSUMERS_SFN_ARN" in m for m in missing)


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


def test_single_warn_when_both_arns_unconfigured(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """When both notification targets are unset, one aggregated warn beats two
    separate ones — avoids doubling log volume on misconfigured deploys."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "")
    log_warn = mocker.patch("task_actions.log.warn")

    notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    # Exactly one aggregated warn, listing both missing targets.
    assert log_warn.call_count == 1
    kwargs = log_warn.call_args.kwargs
    assert "NOTIFY_ASSET_CONSUMERS_SFN_ARN" in str(kwargs.get("missing", []))
    assert "NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN" in str(kwargs.get("missing", []))


def test_lambda_invoke_error_returns_failed(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """An InvokeFunction exception returns ('failed', error_str) — the caller
    is responsible for writing the bell warn. Must not raise."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "arn:fn:notify")
    mock_lambda.invoke.side_effect = RuntimeError("Lambda unreachable")

    result, err = notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert result == "failed"
    assert "Lambda unreachable" in (err or "")
    mock_lambda.invoke.assert_called_once()


def test_empty_outlets_skips_everything(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    result, err = notify_asset_consumers_for_manual_success(
        outlets=[],
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert result == "ok"
    assert err is None
    events_repo.put.assert_not_called()
    mock_sfn.start_execution.assert_not_called()
    mock_lambda.invoke.assert_not_called()


# B2 tests — wake-up failure visibility (ADR #128 §amended)

def test_function_error_in_response_returns_failed(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """FunctionError in the Lambda response payload → ('failed', message).
    The caller (_emit_asset_events_for_manual_success) must write the bell warn."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "arn:fn:notify")
    import io
    mock_lambda.invoke.return_value = {
        "FunctionError": "Unhandled",
        "Payload": io.BytesIO(json.dumps({
            "errorMessage": "out of memory",
            "errorType": "MemoryError",
        }).encode()),
    }

    result, err = notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert result == "failed"
    assert "out of memory" in (err or "")


def test_read_timeout_returns_unknown(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """ReadTimeoutError → ('unknown', message) — outcome unclear, operator must verify."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "arn:fn:notify")
    from botocore.exceptions import ReadTimeoutError
    mock_lambda.invoke.side_effect = ReadTimeoutError(endpoint_url="https://lambda.amazonaws.com")

    result, err = notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert result == "unknown"
    assert "outcome unknown" in (err or "")


def test_successful_invoke_returns_ok(
    mocker, events_repo, mock_sfn, mock_lambda,
):
    """Clean invoke with no FunctionError → ('ok', None)."""
    mocker.patch("task_actions.NOTIFY_ASSET_CONSUMERS_SFN_ARN", "sfn:consumers")
    mocker.patch("task_actions.NOTIFY_ASSET_SUBSCRIBERS_LAMBDA_ARN", "arn:fn:notify")
    import io
    mock_lambda.invoke.return_value = {
        "StatusCode": 200,
        "Payload": io.BytesIO(json.dumps({"notified": 1}).encode()),
    }

    result, err = notify_asset_consumers_for_manual_success(
        outlets=_OUTLETS,
        task_name="t",
        pipeline_name="p",
        date="2026-01-01",
        events_repo=events_repo,
    )

    assert result == "ok"
    assert err is None
