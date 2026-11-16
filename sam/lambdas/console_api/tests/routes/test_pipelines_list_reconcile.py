"""/api/pipelines checks a pipeline's latest "running" run against Step Functions (ADR #112 §7).

The card's status (and the sidebar's "Needs attention") is the latest run's status,
derived from task rows. A run whose execution died keeps "running" rows in DynamoDB, so
without the check the card said "running" forever. /runs already reconciled; the card
did not.

Step Functions is moto (Principle #14: mock the boundary, `config.sfn`); the task rows
come from the DAL, patched the way the other `list_pipelines` tests feed them.
"""
import os

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('DYNAMODB_TABLE', 'test-pipeline-executions')
os.environ.setdefault('PIPELINES_TABLE', 'test-pipeline-registry')

import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

import feed
from routes import pipelines_list

SM_DEFINITION = '{"StartAt": "A", "States": {"A": {"Type": "Pass", "End": true}}}'
TODAY = '2026-11-14'


def _row(run, task, status, started):
    return {'execution_name': f'{task}-{TODAY}--{run}', 'pipeline_name': 'orders',
            'pipeline_execution': f'orders-run-{TODAY}-{run}', 'status': status, 'started_at': started}


@pytest.fixture
def stack(mocker):
    """A moto state machine `orders`, the registry pointing at it, and one day of task rows."""
    with mock_aws():
        sfn = boto3.client('stepfunctions', region_name='us-east-1')
        role = boto3.client('iam', region_name='us-east-1').create_role(
            RoleName='r', AssumeRolePolicyDocument='{}')['Role']['Arn']
        sm_arn = sfn.create_state_machine(name='orders', definition=SM_DEFINITION, roleArn=role)['stateMachineArn']
        mocker.patch('routes.pipelines_list.sfn', sfn)

        frozen = datetime(2026, 11, 14, 13, 0, 0, tzinfo=timezone.utc)
        mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: frozen))
        mocker.patch.object(pipelines_list.pipelines_repo, 'list_all',
                            return_value=[{'pipeline_name': 'orders', 'sfn_arn': sm_arn}])
        rows: list = []
        mocker.patch.object(pipelines_list.executions_repo, 'query_runs_by_date',
                            side_effect=lambda day, **_: rows if day == TODAY else [])

        def start(run):
            sfn.start_execution(stateMachineArn=sm_arn, name=f'orders-run-{TODAY}-{run}')

        yield sfn, rows, start


def _body():
    return json.loads(pipelines_list.list_pipelines({'queryStringParameters': {'stats': 'true'}})['body'])


def _card():
    [card] = _body()['pipelines']
    return card


def test_a_run_whose_execution_failed_is_shown_failed(stack, monkeypatch, mocker):
    _, rows, start = stack
    monkeypatch.setenv('SF_EXECUTION_HISTORY_TYPE', 'FAILURE')  # moto: a new execution starts FAILED
    start('dead0001')
    rows.append(_row('dead0001', 'load', 'running', '2026-11-14T12:00:00Z'))
    mocker.patch.object(pipelines_list.executions_repo, 'query_by_pipeline_execution',
                        return_value=[{'execution_name': 'load-x', 'status': 'running'}])

    card = _card()

    assert card['status'] == 'failed'
    assert card['recent_runs'][0]['status'] == 'failed'


def test_a_run_that_is_really_running_stays_running(stack):
    _, rows, start = stack
    start('live0001')
    rows.append(_row('live0001', 'load', 'running', '2026-11-14T12:00:00Z'))

    assert _card()['status'] == 'running'


def test_a_check_that_fails_keeps_the_derived_status(stack):
    """No such execution (ExecutionDoesNotExist): reconcile_sfn_status logs and returns None."""
    _, rows, _ = stack
    rows.append(_row('gone0001', 'load', 'running', '2026-11-14T12:00:00Z'))

    assert _card()['status'] == 'running'


def test_only_the_latest_run_is_checked(stack, mocker):
    sfn, rows, start = stack
    start('new00001')
    rows.extend([
        _row('old00001', 'load', 'running', '2026-11-14T08:00:00Z'),
        _row('new00001', 'load', 'running', '2026-11-14T12:00:00Z'),
    ])
    describe = mocker.spy(sfn, 'describe_execution')

    card = _card()

    assert [c.kwargs['executionArn'].rsplit(':', 1)[-1] for c in describe.call_args_list] == [f'orders-run-{TODAY}-new00001']
    assert [r['status'] for r in card['recent_runs']] == ['running', 'running']


def test_a_finished_latest_run_needs_no_check(stack, mocker):
    sfn, rows, _ = stack
    rows.append(_row('done0001', 'load', 'success', '2026-11-14T12:00:00Z'))
    describe = mocker.spy(sfn, 'describe_execution')

    assert _card()['status'] == 'success'
    assert describe.call_count == 0


def test_a_run_waiting_for_a_decision_says_so(stack):
    _, rows, start = stack
    start('wait0001')
    rows.extend([
        _row('wait0001', 'extract', 'success', '2026-11-14T12:00:00Z'),
        _row('wait0001', 'approve', 'waiting_decision', '2026-11-14T12:05:00Z'),
    ])

    card = _card()

    assert card['status'] == 'running'
    assert card['waiting_on'] == 'waiting_decision'


def test_a_paused_run_says_so(stack):
    _, rows, start = stack
    start('paus0001')
    rows.append(_row('paus0001', 'load', 'waiting_paused', '2026-11-14T12:00:00Z'))

    assert _card()['waiting_on'] == 'waiting_paused'


def test_a_failed_run_waits_on_nobody(stack, monkeypatch, mocker):
    _, rows, start = stack
    monkeypatch.setenv('SF_EXECUTION_HISTORY_TYPE', 'FAILURE')
    start('dead0002')
    rows.append(_row('dead0002', 'approve', 'waiting_decision', '2026-11-14T12:00:00Z'))
    mocker.patch.object(pipelines_list.executions_repo, 'query_by_pipeline_execution',
                        return_value=[{'execution_name': 'approve-x', 'status': 'waiting_decision'}])

    card = _card()

    assert card['status'] == 'failed'
    assert card['waiting_on'] is None


def test_past_the_time_budget_the_status_stays_derived_and_the_answer_says_so(stack, mocker):
    sfn, rows, start = stack
    start('slow0001')
    rows.append(_row('slow0001', 'load', 'running', '2026-11-14T12:00:00Z'))
    mocker.patch.object(pipelines_list, 'RECONCILE_BUDGET_SECONDS', -1)
    describe = mocker.spy(sfn, 'describe_execution')

    body = _body()

    assert body['pipelines'][0]['status'] == 'running'
    assert body['stats_incomplete'] is True
    assert describe.call_count == 0


def test_a_date_that_cannot_be_read_marks_the_stats_incomplete(stack, mocker):
    _, rows, _ = stack
    rows.append(_row('done0002', 'load', 'success', '2026-11-14T12:00:00Z'))

    def query(day, **_):
        if day == '2026-11-13':
            raise ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException', 'Message': 'slow down'}}, 'Query')
        return rows if day == TODAY else []
    mocker.patch.object(pipelines_list.executions_repo, 'query_runs_by_date', side_effect=query)

    body = _body()

    assert body['stats_incomplete'] is True
    assert body['pipelines'][0]['status'] == 'success'


def test_stats_are_complete_when_every_read_and_check_succeeded(stack):
    _, rows, start = stack
    start('live0002')
    rows.append(_row('live0002', 'load', 'running', '2026-11-14T12:00:00Z'))

    assert _body()['stats_incomplete'] is False


def test_a_recovered_run_counts_as_a_success_in_the_sla(stack, monkeypatch, mocker):
    """ADR #112: recovered = Step Functions failed but every task succeeded."""
    _, rows, start = stack
    monkeypatch.setenv('SF_EXECUTION_HISTORY_TYPE', 'FAILURE')
    start('reco0001')
    rows.append(_row('reco0001', 'load', 'running', '2026-11-14T12:00:00Z'))
    mocker.patch.object(pipelines_list.executions_repo, 'query_by_pipeline_execution',
                        return_value=[{'execution_name': 'load-x', 'status': 'success'}])

    card = _card()

    assert card['status'] == 'recovered'
    assert card['sla'] == 100


def test_the_console_step_functions_client_fails_inside_the_request():
    """Principle #53: well inside API Gateway's 29 s, even with every retry."""
    import config
    cfg = config.SFN_CLIENT_CONFIG
    worst_call = cfg['retries']['total_max_attempts'] * (cfg['connect_timeout'] + cfg['read_timeout'])
    reads_before = 9  # the 14 date queries and the registry read, with room to spare
    assert pipelines_list.RECONCILE_BUDGET_SECONDS + worst_call + reads_before <= 29
