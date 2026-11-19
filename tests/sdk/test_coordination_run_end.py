"""How a run ends (ADR #137 §4, ADR #138 §5)."""

import pytest

from polyris.constants import ExecutionStatus
from polyris.coordination import run_end


from polyris.constants import _DERIVE_TERMINAL
from polyris.coordination.transitions import FINAL


def test_a_summary_counts_the_rows_per_status():
    assert run_end.summary(["success", "failed", "success"]) == {"success": 2, "failed": 1}


@pytest.mark.parametrize("counts,step_failed,expected", [
    ({"success": 3, "skipped": 1}, False, ExecutionStatus.SUCCESS),
    ({"success": 3, "failed": 1}, False, ExecutionStatus.FAILED),
    ({"success": 2, "upstream_failed": 1, "stopped": 1}, False, ExecutionStatus.ABORTED),
    ({"failed": 1, "stopped": 3}, False, ExecutionStatus.FAILED),         # a failure outranks a stop
    ({"success": 4}, True, ExecutionStatus.FAILED),                        # a failed direct step
    ({"success": 2, "failed": 1, "running": 1}, False, None),             # a row still runs: no answer
    ({"success": 3, "waiting_decision": 1}, False, None),
    ({"success": 4, "failed": 0}, False, ExecutionStatus.SUCCESS),        # a zero count is no row
])
def test_the_outcome_waits_for_every_row_to_be_final(counts, step_failed, expected):
    assert run_end.outcome(counts, task_count=4, step_failed=step_failed) == expected


@pytest.mark.parametrize("counts", [{"success": 3}, {}, {"success": 5}])
def test_a_read_that_missed_rows_answers_nothing(counts):
    """Invariant 10: the counts must cover every row of the run."""
    assert run_end.outcome(counts, task_count=4, step_failed=False) is None


def test_a_cleanup_that_succeeded_after_a_failure_does_not_make_the_run_succeed():
    """ADR #138 §5: a run with a final failure fails."""
    assert run_end.outcome({"failed": 1, "success": 1}, task_count=2, step_failed=False) == ExecutionStatus.FAILED


def test_the_derivation_and_the_core_agree_on_what_is_final():
    """outcome hands only final rows to derive_execution_status, which must count them all as finished."""
    assert _DERIVE_TERMINAL == FINAL


def test_the_cause_names_the_failed_tasks_and_a_failed_step():
    assert run_end.failure_cause(["load", "extract"]) == "2 tasks failed: extract, load"
    assert run_end.failure_cause([], step_failure="notify") == "step notify failed; 0 tasks failed"


def test_the_cause_of_a_large_run_stays_within_step_functions_limit():
    names = [f"task_with_a_long_name_{i:05d}" for i in range(3000)]
    cause = run_end.failure_cause(names)
    assert len(cause) <= run_end.CAUSE_MAX
    listed = cause.split(": ", 1)[1].rsplit(" and ", 1)
    assert listed[1].endswith(" more") and int(listed[1].split()[0]) == 3000 - len(listed[0].split(", "))


@pytest.mark.parametrize("stored,read_at,written", [
    (None, "2026-11-18T06:00:00.000Z", True),                              # the first summary
    ("2026-11-18T06:00:00.000Z", "2026-11-18T06:00:05.000Z", True),        # a pass that read later
    ("2026-11-18T06:00:05.000Z", "2026-11-18T06:00:00.000Z", False),       # a slow pass: the newer count stays
])
def test_the_summarys_condition_as_dynamodb_evaluates_it(stored, read_at, written):
    import boto3
    from botocore.exceptions import ClientError
    from moto import mock_aws

    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="tokens", KeySchema=[{"AttributeName": "execution_name", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "execution_name", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST")
        key = {"execution_name": "_run_orders-run-2026-11-18-abcd1234"}
        table.put_item(Item={**key, **({"summary_at": stored, "summary": {"running": 1}} if stored else {})})
        try:
            table.update_item(Key=key, UpdateExpression="SET #summary = :summary, #summary_at = :read_at",
                              ConditionExpression=run_end.SUMMARY_CONDITION,
                              ExpressionAttributeNames=run_end.SUMMARY_NAMES,
                              ExpressionAttributeValues={":summary": {"success": 1}, ":read_at": read_at})
            applied = True
        except ClientError as exc:
            assert exc.response["Error"]["Code"] == "ConditionalCheckFailedException"
            applied = False
    assert applied is written
