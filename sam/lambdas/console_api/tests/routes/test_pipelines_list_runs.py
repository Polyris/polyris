"""The pipelines list's stats (`/api/pipelines?stats=true`) on a moto tokens table (ADR #137 §5, D94; ADR #136).

The stats read the run rows of the last 14 start days from run-start-index; a
pipeline's status and `waiting_on` come from its latest run's summary, and its
`recent_runs` are the runs whose logical date is within the 14 days, newest first by
date, then by start. The console asks Step Functions nothing about a run.
`stats_incomplete` says a start day could not be read. Only the tables, the clock and
the Step Functions client are faked (Principle #14).
"""
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.runs_kit import NOW, days_ago, freeze, kit, open_registry, open_tables, put_run, put_task

TODAY = NOW.strftime('%Y-%m-%d')


@pytest.fixture
def table(mocker):
    from routes import pipelines_list

    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        table = open_tables(mocker, resource)
        freeze(mocker)
        open_registry(mocker, resource,
                      {'pipeline_name': 'orders', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:orders'},
                      {'pipeline_name': 'billing', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:billing'})
        yield table
    assert not hasattr(pipelines_list, 'sfn'), "the console asks Step Functions nothing about a run (ADR #137 §5)"


def body():
    from routes import pipelines_list

    return json.loads(pipelines_list.list_pipelines({'queryStringParameters': {'stats': 'true'}})['body'])


def card(name='orders'):
    return next(p for p in body()['pipelines'] if p['name'] == name)


def test_the_card_shows_its_latest_run_from_the_run_rows_summary(table):
    put_run(table, 'orders', '2026-11-21', days_ago(1), suffix='old00001', summary={'failed': 1, 'success': 1})
    put_run(table, 'orders', TODAY, days_ago(0), suffix='new00001', summary={'success': 2})
    orders = card()
    assert (orders['status'], orders['failed_tasks'], orders['waiting_on']) == ('success', 0, None)
    assert [(r['exec'], r['status']) for r in orders['recent_runs']] == [('new00001', 'success'), ('old00001', 'failed')]
    assert body()['stats_incomplete'] is False and body()['index_building'] is False


def test_the_card_shows_the_latest_run_of_a_date_with_several(table):
    """Seen live (1.2.0 upgrade rehearsal): runs of one date came in index order, and the card said "failed"."""
    put_run(table, 'orders', TODAY, days_ago(0, hour=8), suffix='3793baa1', summary={'failed': 2})
    put_run(table, 'orders', TODAY, days_ago(0, hour=9), suffix='a1223ca1', summary={'success': 2})
    orders = card()
    assert orders['status'] == 'success'
    assert [r['exec'] for r in orders['recent_runs']] == ['a1223ca1', '3793baa1']


def test_a_running_run_with_a_failed_task_says_how_many_and_who_it_waits_for(table):
    """§4, D68: running until every row is final; waiting_on from the summary (ADR #136)."""
    put_run(table, 'orders', TODAY, days_ago(0), summary={'failed': 1, 'waiting_decision': 1, 'waiting': 2})
    put_run(table, 'billing', TODAY, days_ago(0), summary={'success': 1, 'waiting_paused': 1})
    orders, billing = card('orders'), card('billing')
    assert (orders['status'], orders['failed_tasks'], orders['waiting_on']) == ('running', 1, 'waiting_decision')
    assert (billing['status'], billing['failed_tasks'], billing['waiting_on']) == ('running', 0, 'waiting_paused')
    assert orders['recent_runs'][0]['failed_tasks'] == 1


def test_a_finished_run_waits_on_nobody(table):
    put_run(table, 'orders', TODAY, days_ago(0), summary={'failed': 1, 'success': 1})
    assert (card()['status'], card()['waiting_on']) == ('failed', None)


def test_a_pipeline_without_a_run_in_the_window_is_idle_and_runs_from_before_1_3_do_not_count(table):
    """§5, *Migration and upgrade*: a 1.2 run has no run row; the pipeline's status appears with its first 1.3 run."""
    put_task(table, 'orders-run-2026-11-21-legacy01', 'extract', '2026-11-21', 'failed', started=days_ago(1))
    put_run(table, 'orders', '2026-11-01', days_ago(21), suffix='past0001', summary={'success': 1})
    orders = card()
    assert (orders['status'], orders['recent_runs'], orders['sla']) == ('idle', None, None)


def test_recent_runs_are_the_runs_whose_date_is_within_the_window_newest_first(table):
    """A rerun of an old date, started today, is no recent run; a run dated ahead of its start is (ADR #137 §5)."""
    put_run(table, 'orders', '2026-10-01', days_ago(0), suffix='rerun001', summary={'success': 1})
    put_run(table, 'orders', '2026-11-23', days_ago(0), suffix='ahead001', summary={'success': 1})
    put_run(table, 'orders', '2026-11-15', days_ago(7), suffix='middle01', summary={'success': 1})
    put_run(table, 'orders', '2026-11-09', days_ago(13), suffix='edge0001', summary={'failed': 1})
    assert [r['exec'] for r in card()['recent_runs']] == ['ahead001', 'middle01', 'edge0001']


def test_recent_runs_are_the_last_ten(table):
    for i in range(12):
        put_run(table, 'orders', f'2026-11-{10 + i:02d}', days_ago(12 - i), suffix=f'daily{i:03d}', summary={'success': 1})
    assert [r['exec'] for r in card()['recent_runs']] == [f'daily{i:03d}' for i in range(11, 1, -1)]


def test_the_sla_counts_only_ended_runs_that_were_not_stopped(table):
    """ADR #112: a stop is not a system failure; a running run has no outcome yet."""
    put_run(table, 'orders', TODAY, days_ago(0, hour=1), suffix='ok000001', summary={'success': 1})
    put_run(table, 'orders', TODAY, days_ago(0, hour=2), suffix='stop0001', summary={'stopped': 1})
    put_run(table, 'orders', TODAY, days_ago(0, hour=3), suffix='fail0001', summary={'failed': 1})
    put_run(table, 'orders', TODAY, days_ago(0, hour=4), suffix='run00001', summary={'running': 1})
    assert card()['sla'] == 50


def _query_failing_on(mocker, start_day, error):
    """The table's query, failing for one start day's read of run-start-index (the boto3 boundary)."""
    from dal import executions_repo

    real = type(executions_repo).table.fget(executions_repo)

    class Table:
        def query(self, **kwargs):
            if kwargs.get('IndexName') == 'run-start-index' and start_day in str(kwargs['KeyConditionExpression']
                                                                                .get_expression()['values']):
                raise error
            return real.query(**kwargs)

    mocker.patch.object(type(executions_repo), 'table', new_callable=mocker.PropertyMock, return_value=Table())


def test_a_start_day_that_cannot_be_read_marks_the_stats_incomplete(table, mocker):
    put_run(table, 'orders', TODAY, days_ago(0), summary={'success': 1})
    _query_failing_on(mocker, days_ago(3).strftime('%Y-%m-%d'),
                      ClientError({'Error': {'Code': 'ProvisionedThroughputExceededException', 'Message': ''}}, 'Query'))
    answer = body()
    assert answer['stats_incomplete'] is True
    assert next(p for p in answer['pipelines'] if p['name'] == 'orders')['status'] == 'success'


def test_a_denied_read_fails_the_request(table, mocker):
    """ADR #136 §4: a call the console API may not make is raised, never shown as a stale status."""
    _query_failing_on(mocker, TODAY, ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'no'}}, 'Query'))
    with pytest.raises(ClientError, match='AccessDeniedException'):
        body()


def test_while_the_index_is_built_the_list_says_so_and_keeps_its_pipelines(table, mocker):
    """§5: transient, and said: the pipelines show, their statuses once the index is ready."""
    _query_failing_on(mocker, TODAY, ClientError({'Error': {'Code': 'ValidationException', 'Message':
                                                            'Cannot read from backfilling global secondary index'}},
                                                 'Query'))
    answer = body()
    assert (answer['stats_incomplete'], answer['index_building']) == (True, True)
    assert {p['name'] for p in answer['pipelines']} == {'orders', 'billing'}


def test_without_stats_nothing_reads_the_runs(table, mocker):
    from dal import executions_repo
    from routes import pipelines_list

    read = mocker.spy(executions_repo, 'runs_started')
    pipelines_list.list_pipelines({'queryStringParameters': None})
    read.assert_not_called()


def test_a_registry_that_cannot_be_read_fails_the_request(table):
    """ADR #136: a list without the registry has no groups and misses idle pipelines; never a 200."""

    boto3.resource('dynamodb', region_name='us-east-1').Table(kit.TABLES['PIPELINES_TABLE'][1]).delete()
    with pytest.raises(ClientError, match='ResourceNotFoundException'):
        body()
