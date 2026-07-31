"""reconcile_sfn_status against a real (moto) Step Functions backend.

Found auditing this session's own mocks: every test that touches this function mocks
it directly (a legitimate isolation in *those* tests, since they're about paging/feed
logic, not reconciliation) — but nowhere in the codebase was it ever exercised for
real. Its own decision logic (`reconcile_execution_status`, called at the end) is
well-tested in isolation (`tests/sdk/test_execution_status.py`), but the I/O wrapper
around it — the ARN construction, the SFN describe call, the task-resolution query,
and both silent `except` branches — had zero coverage against anything real.

Per Principle #14: mock at the boundary (`boto3` via moto), never the function itself.
"""
import os

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DYNAMODB_TABLE', 'test-pipeline-executions')
os.environ.setdefault('PIPELINES_TABLE', 'test-pipeline-registry')

import boto3
import pytest
from moto import mock_aws

from routes.pipelines_list import reconcile_sfn_status


SM_DEFINITION = '{"StartAt": "A", "States": {"A": {"Type": "Pass", "End": true}}}'


@pytest.fixture
def sfn_execution(mocker):
    """A real state machine + execution in moto, wired so `config.sfn` (the module's
    lazily-resolved boto3 client) talks to it — the same client `reconcile_sfn_status`
    calls through.

    moto does not run a state machine's own definition to derive its terminal status;
    a fresh execution's status is controlled process-wide by ``SF_EXECUTION_HISTORY_TYPE``
    (moto's own toggle — ``SUCCESS`` → stays ``RUNNING``, ``FAILURE`` → starts ``FAILED``).
    ``stop_execution`` produces ``ABORTED``, a different terminal status than a task
    failure, so it is not a substitute for the ``FAILURE`` setting here.
    """
    with mock_aws():
        sfn = boto3.client('stepfunctions', region_name='us-east-1')
        role = boto3.client('iam', region_name='us-east-1').create_role(
            RoleName='r', AssumeRolePolicyDocument='{}')['Role']['Arn']
        sm_arn = sfn.create_state_machine(
            name='sales-hourly', definition=SM_DEFINITION, roleArn=role)['stateMachineArn']

        def start(name):
            return sfn.start_execution(stateMachineArn=sm_arn, name=name)['executionArn']

        mocker.patch('routes.pipelines_list.sfn', sfn)
        yield sm_arn, start


@pytest.fixture
def failing_executions(monkeypatch):
    """Scopes moto's FAILURE toggle to tests that need a terminally-failed execution,
    so the RUNNING-path tests above are unaffected."""
    monkeypatch.setenv('SF_EXECUTION_HISTORY_TYPE', 'FAILURE')


class TestReconcileSfnStatusAgainstRealSfn:
    def test_a_running_execution_stays_running(self, sfn_execution):
        sm_arn, start = sfn_execution
        start('run-1')

        assert reconcile_sfn_status('run-1', sm_arn) == 'running'

    def test_a_failed_execution_with_no_pending_tasks_reports_failed(
            self, sfn_execution, failing_executions, mocker):
        sm_arn, start = sfn_execution
        start('run-2')
        mocker.patch('routes.pipelines_list.executions_repo.query_by_pipeline_execution',
                    return_value=[{'status': 'success'}, {'status': 'failed'}])

        assert reconcile_sfn_status('run-2', sm_arn) == 'failed'

    def test_a_failed_execution_whose_tasks_all_succeeded_recovers(
            self, sfn_execution, failing_executions, mocker):
        """ADR #112's recovered rule: the orchestrator failed but every task actually
        resolved — this is the one path `reconcile_execution_status` treats specially,
        and the one this wrapper exists to feed it `all_resolved` for."""
        sm_arn, start = sfn_execution
        start('run-3')
        mocker.patch('routes.pipelines_list.executions_repo.query_by_pipeline_execution',
                    return_value=[{'status': 'success'}, {'status': 'success'}])

        assert reconcile_sfn_status('run-3', sm_arn) == 'recovered'

    def test_no_arn_short_circuits_without_calling_sfn(self):
        assert reconcile_sfn_status('run-x', '') is None

    def test_an_execution_that_no_longer_exists_degrades_to_none(self, sfn_execution, mocker):
        """The outer except: describe_execution on a made-up name raises ClientError
        (ExecutionDoesNotExist) — must degrade to None (caller keeps the DDB status),
        not raise, and — per ADR #38 — must now log rather than swallow silently."""
        sm_arn, _ = sfn_execution
        warn = mocker.patch('routes.pipelines_list.log.warn')

        result = reconcile_sfn_status('never-started', sm_arn)

        assert result is None
        warn.assert_called_once()
        assert warn.call_args.args[0] == 'reconcile_sfn_status'

    def test_task_lookup_failure_still_returns_a_status_and_logs(
            self, sfn_execution, failing_executions, mocker):
        """The inner except: task-resolution lookup fails, but the SFN status itself
        was readable — must still return a reconciled status (not None), treating
        `all_resolved` as False, and must log the degradation (ADR #38)."""
        from botocore.exceptions import ClientError
        sm_arn, start = sfn_execution
        start('run-4')
        mocker.patch('routes.pipelines_list.executions_repo.query_by_pipeline_execution',
                    side_effect=ClientError({'Error': {'Code': 'Throttling'}}, 'Query'))
        warn = mocker.patch('routes.pipelines_list.log.warn')

        result = reconcile_sfn_status('run-4', sm_arn)

        assert result == 'failed'          # all_resolved defaulted to False, not crashed
        assert warn.call_count == 1
        assert warn.call_args.args[0] == 'reconcile_sfn_status'
