"""The run row's contract beyond its keys (ADR #137 §3, §9)."""

import re

import pytest

from polyris.constants import BackfillCascade, WarningKind
from polyris.coordination import run_row, statuses
from polyris.xcom import ENV_UPSTREAM_STATUSES


@pytest.mark.parametrize("suppress,cascade_all,expected", [
    (False, False, BackfillCascade.AUTO),
    (False, True, BackfillCascade.ALL),
    (True, False, BackfillCascade.NONE),
    (True, True, BackfillCascade.NONE),          # no fact reaches a consumer: none wins
])
def test_the_runs_cascade_from_its_starters_flags(suppress, cascade_all, expected):
    assert run_row.cascade(suppress_asset_event=suppress, cascade_all=cascade_all) == expected


def test_a_failed_runs_answer_has_an_error_name_of_its_own():
    """States.* names are Step Functions' own, and a Retry on States.ALL would match it."""
    assert run_row.FAILED_RUN_ERROR.startswith("Polyris.")
    assert not run_row.FAILED_RUN_ERROR.startswith("States.")


def test_the_input_item_keeps_the_runs_three_fields():
    assert {run_row.VARIABLES, run_row.SKIP_TASKS, run_row.CASCADE} == {"variables", "skip_tasks", "cascade"}


def test_a_failed_direct_step_has_a_warning_kind_of_its_own():
    assert WarningKind.STEP == "step" and "step" in statuses.WARNING_KINDS


def test_the_upstream_statuses_reach_a_task_under_an_environment_name():
    assert re.fullmatch(r"POLYRIS_[A-Z_]+", ENV_UPSTREAM_STATUSES)


# ── the order of Start_Run's tokens (§1) ─────────────────────────────

def test_a_redrive_orders_after_every_retry_of_the_attempts_before_it():
    assert run_row.token_order(redrive_count=1, retry_count=0) > run_row.token_order(redrive_count=0,
                                                                                    retry_count=99_999_999)
    assert run_row.token_order(redrive_count=0, retry_count=1) > run_row.token_order(redrive_count=0, retry_count=0)


@pytest.mark.parametrize("redrive,retry", [(-1, 0), (0, -1), (0, 100_000_000)])
def test_counters_out_of_step_functions_range_are_refused(redrive, retry):
    with pytest.raises(ValueError, match="counters out of range"):
        run_row.token_order(redrive_count=redrive, retry_count=retry)


@pytest.mark.parametrize("stored,attempt,accepted", [
    ({}, (0, 0), True),                                                # the first token
    ({"token_order": (0, 2)}, (0, 3), True),                           # a later retry
    ({"token_order": (0, 3)}, (0, 3), True),                           # the same attempt writes again
    ({"token_order": (0, 3)}, (0, 2), False),                          # an earlier attempt, late
    ({"token_order": (0, 7)}, (1, 0), True),                           # a redrive
    ({"token_order": (0, 2), "stop": {"by": "engine"}}, (0, 3), False),  # the engine stopped the run
])
def test_the_token_condition_as_dynamodb_evaluates_it(stored, attempt, accepted):
    """The real condition, evaluated by DynamoDB (moto)."""
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    def order(pair):
        return run_row.token_order(redrive_count=pair[0], retry_count=pair[1])

    row = {**stored, **({"token_order": order(stored["token_order"])} if "token_order" in stored else {})}
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        key = {"execution_name": run_row.key("orders-run-2026-11-18-abcd1234")}
        table.put_item(Item={**key, **row})
        try:
            table.update_item(Key=key, UpdateExpression="SET #order = :order, run_token = :token",
                              ConditionExpression=run_row.TOKEN_CONDITION,
                              ExpressionAttributeNames=run_row.TOKEN_CONDITION_NAMES,
                              ExpressionAttributeValues={":order": order(attempt), ":token": "t"})
            written = True
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
            written = False
    assert written is accepted


# ── run_version (§1, §3) ─────────────────────────────────────────────

def _table():
    import boto3

    return boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")


def test_a_decision_read_before_a_raise_loses_its_transaction():
    """A Restart lands between a pass's read and its write: the decision's ConditionCheck fails."""
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    run_key = {"execution_name": run_row.key("orders-run-2026-11-18-abcd1234")}
    with mock_aws():
        table = _table()
        table.put_item(Item={**run_key, "run_version": 4})
        read = 4
        table.update_item(Key=run_key, UpdateExpression=f"SET {run_row.VERSION_RAISE}",       # the Restart
                          ExpressionAttributeNames=run_row.VERSION_NAMES,
                          ExpressionAttributeValues=run_row.VERSION_RAISE_VALUES)
        client = boto3.client("dynamodb", region_name="us-east-1")
        with pytest.raises(ClientError, match="TransactionCanceled"):
            client.transact_write_items(TransactItems=[
                {"ConditionCheck": {"TableName": "tokens", "Key": {"execution_name": {"S": run_key["execution_name"]}},
                                    "ConditionExpression": run_row.VERSION_UNCHANGED,
                                    "ExpressionAttributeNames": run_row.VERSION_NAMES,
                                    "ExpressionAttributeValues": {":rv": {"N": str(read)}}}},
                {"Put": {"TableName": "tokens", "Item": {"execution_name": {"S": "load-2026-11-18-x"},
                                                          "status": {"S": "deps_ready"}}}},
            ])


def test_a_passs_own_raise_holds_only_while_nothing_else_raised():
    from botocore.exceptions import ClientError
    from moto import mock_aws

    run_key = {"execution_name": run_row.key("orders-run-2026-11-18-abcd1234")}
    with mock_aws():
        table = _table()
        table.put_item(Item={**run_key, "run_version": 7})
        raise_from = dict(Key=run_key, UpdateExpression=f"SET {run_row.VERSION_NEXT}",
                          ConditionExpression=run_row.VERSION_UNCHANGED,
                          ExpressionAttributeNames=run_row.VERSION_NAMES,
                          ExpressionAttributeValues=run_row.version_values(7))
        table.update_item(**raise_from)
        assert table.get_item(Key=run_key)["Item"]["run_version"] == 8
        with pytest.raises(ClientError, match="ConditionalCheckFailed"):
            table.update_item(**raise_from)                    # the pass read 7: someone raised since


def test_an_action_raises_a_row_that_has_no_run_version_yet():
    from moto import mock_aws

    run_key = {"execution_name": run_row.key("orders-run-2026-11-18-abcd1234")}
    with mock_aws():
        table = _table()
        table.put_item(Item=run_key)
        table.update_item(Key=run_key, UpdateExpression=f"SET {run_row.VERSION_RAISE}",
                          ExpressionAttributeNames=run_row.VERSION_NAMES,
                          ExpressionAttributeValues=run_row.VERSION_RAISE_VALUES)
        assert table.get_item(Key=run_key)["Item"]["run_version"] == 1


# ── run-start-index (§5) ─────────────────────────────────────────────

def test_the_index_keys_are_strings_and_the_day_is_the_starts_utc_day():
    from datetime import datetime, timedelta, timezone

    kyiv = timezone(timedelta(hours=2))
    keys = run_row.index_keys(datetime(2026, 11, 19, 1, 30, tzinfo=kyiv))   # 23:30 UTC the day before
    assert keys == {"start_day": "2026-11-18", "started_at": "2026-11-18T23:30:00.000Z"}
    assert all(isinstance(v, str) for v in keys.values())


def test_the_index_keys_refuse_a_naive_time():
    from datetime import datetime

    with pytest.raises(ValueError, match="timezone-aware"):
        run_row.index_keys(datetime(2026, 11, 18, 6, 0))
