"""The Runs tab (`/api/runs`) on a moto tokens table (ADR #137 §4, §5; ADR #95).

Without a date it reads run rows from run-start-index: the start days of the last
90 days, newest first, until the page is full; with a pipeline filter the same days.
A date filter finds its runs through the task rows of that date, runs from before 1.3
included. Each run's status comes from its run row's summary. Backfills merge in by
`started_at`. Only the DynamoDB tables and the clock are faked (Principle #14).
"""
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from constants import Limits
from tests.runs_kit import days_ago, freeze, iso, open_tables, put_run, put_task, NOW

TODAY = NOW.strftime('%Y-%m-%d')


@pytest.fixture
def table(mocker):
    with mock_aws():
        table = open_tables(mocker, boto3.resource('dynamodb', region_name='us-east-1'))
        freeze(mocker)
        yield table


def runs(**query):
    from routes import executions

    response = executions.get_all_runs({'queryStringParameters': {k: str(v) for k, v in query.items()} or None})
    return response['statusCode'], json.loads(response['body'])


def names(body):
    return [r.get('pipeline_execution') or r.get('backfill_id') for r in body['runs']]


def test_the_feed_lists_run_rows_newest_first_with_their_status_from_the_summary(table):
    old = put_run(table, 'orders', '2026-11-20', days_ago(2), suffix='old00001', summary={'success': 3},
                  finished=days_ago(2, hour=7), triggered_by='console_run')
    new = put_run(table, 'billing', TODAY, days_ago(0), suffix='new00001', summary={'success': 1, 'running': 1})
    status, body = runs()
    assert status == 200 and names(body) == [new, old]
    first, second = body['runs']
    assert (first['kind'], first['status'], first['failed_tasks'], first['finished_at'], first['duration_ms']) == \
        ('execution', 'running', 0, None, None)
    assert (second['status'], second['date'], second['triggered_by'], second['duration_ms']) == \
        ('success', '2026-11-20', 'console_run', 3600 * 1000)
    assert second['pipeline_execution_short'] == old[-20:]


def test_a_run_with_a_failed_task_is_running_with_the_count_until_every_row_is_final(table):
    """§4, D68."""
    put_run(table, 'orders', TODAY, days_ago(0), suffix='mixed001', summary={'failed': 2, 'waiting': 1})
    put_run(table, 'orders', TODAY, days_ago(0, hour=5), suffix='done0001', summary={'failed': 1, 'success': 2})
    put_run(table, 'orders', TODAY, days_ago(0, hour=4), suffix='stop0001', summary={'stopped': 1, 'success': 2})
    put_run(table, 'orders', TODAY, days_ago(0, hour=3), suffix='step0001', summary={'success': 2},
            step_failure={'step': 'notify', 'error': 'boom'})
    put_run(table, 'orders', TODAY, days_ago(0, hour=2), suffix='new00001')            # no summary yet
    status, body = runs()
    assert [(r['status'], r['failed_tasks']) for r in body['runs']] == [
        ('running', 2), ('failed', 1), ('aborted', 0), ('failed', 0), ('running', 0)]


def test_a_run_from_before_1_3_has_no_run_row_and_is_not_in_the_feed_without_a_date(table):
    legacy = 'orders-run-2026-11-21-legacy01'
    put_task(table, legacy, 'extract', '2026-11-21', 'success', started=days_ago(1))
    assert names(runs()[1]) == []


def test_the_window_is_ninety_start_days_and_a_rerun_of_an_old_date_started_today_comes_first(table):
    rerun = put_run(table, 'orders', '2026-01-02', days_ago(0), suffix='rerun001', summary={'success': 1})
    edge = put_run(table, 'orders', '2026-08-25', days_ago(Limits.RUNS_WINDOW_DAYS - 1), suffix='edge0001',
                   summary={'success': 1})
    put_run(table, 'orders', '2026-08-24', days_ago(Limits.RUNS_WINDOW_DAYS), suffix='past0001', summary={'success': 1})
    assert Limits.RUNS_WINDOW_DAYS == 90
    assert names(runs()[1]) == [rerun, edge]


def test_the_walk_stops_once_the_page_is_full(table, mocker):
    """The shard key is the start day: no run on an older day started later, so older days are not read."""
    from dal import executions_repo

    for i in range(3):
        put_run(table, 'orders', TODAY, days_ago(0, hour=i + 1), suffix=f'today{i:03d}', summary={'success': 1})
    put_run(table, 'orders', '2026-10-01', days_ago(50), suffix='older001', summary={'success': 1})
    read = mocker.spy(executions_repo, 'runs_started')
    status, body = runs(limit=2)
    assert len(body['runs']) == 2 and body['next'] == body['runs'][-1]['started_at']
    assert [c.args[0] for c in read.call_args_list] == [TODAY]


def test_the_cursor_pages_through_start_days_without_repeats(table):
    made = [put_run(table, 'orders', f'2026-11-{20 - i:02d}', days_ago(i * 3), suffix=f'run{i:05d}',
                    summary={'success': 1}) for i in range(5)]
    seen, cursor = [], None
    for _ in range(5):
        status, body = runs(limit=2, **({'before': cursor} if cursor else {}))
        seen += names(body)
        cursor = body['next']
        if cursor is None:
            break
    assert seen == made


def test_a_pipeline_filter_walks_the_same_start_days_for_that_pipeline_only(table):
    mine = put_run(table, 'orders', '2026-10-01', days_ago(60), suffix='mine0001', summary={'success': 1})
    put_run(table, 'billing', TODAY, days_ago(0), suffix='other001', summary={'success': 1})
    status, body = runs(pipeline='orders')
    assert names(body) == [mine] and body['filters']['pipeline'] == 'orders'


def test_a_status_filter_counts_only_the_runs_it_keeps(table):
    """The status is derived from the summary, so a page of failed runs among successes still fills and pages."""
    failed = [put_run(table, 'orders', TODAY, days_ago(i), suffix=f'fail{i:04d}', summary={'failed': 1})
              for i in (1, 5, 9)]
    for i in range(12):
        put_run(table, 'orders', TODAY, days_ago(0, hour=i + 1), suffix=f'ok{i:06d}', summary={'success': 1})
    status, body = runs(status='failed', limit=2)
    assert names(body) == failed[:2] and body['next'] is not None
    assert names(runs(status='failed', limit=2, before=body['next'])[1]) == failed[2:]


def test_a_date_filter_reads_the_task_rows_of_the_date_and_the_run_rows_of_its_runs(table):
    """A date has no window, and finds runs from before 1.3, whose status comes from their task rows."""
    run = put_run(table, 'orders', '2026-03-01', days_ago(0), suffix='new13001', summary={'failed': 1, 'running': 1},
                  triggered_by='backfill')
    put_task(table, run, 'extract', '2026-03-01', 'failed')
    put_task(table, run, 'load', '2026-03-01', 'success')          # the summary, not the rows, says the status
    legacy = 'orders-run-2026-03-01-legacy01'
    put_task(table, legacy, 'extract', '2026-03-01', 'failed', started=days_ago(200), finished=days_ago(200, 7),
             triggered_by='schedule')
    put_task(table, legacy, 'load', '2026-03-01', 'upstream_failed', started=days_ago(200), triggered_by='schedule')
    status, body = runs(date='2026-03-01')
    by_name = {r['pipeline_execution']: r for r in body['runs']}
    assert (by_name[run]['status'], by_name[run]['failed_tasks'], by_name[run]['triggered_by']) == \
        ('running', 1, 'backfill')
    assert (by_name[legacy]['status'], by_name[legacy]['failed_tasks'], by_name[legacy]['triggered_by']) == \
        ('failed', 1, 'schedule')
    assert by_name[legacy]['started_at'] == iso(days_ago(200)) and '_sample_exec_name' not in by_name[legacy]
    assert names(body) == [run, legacy]


def test_a_date_filter_with_a_pipeline_reads_that_pipelines_rows(table):
    mine = put_run(table, 'orders', '2026-03-01', days_ago(0), suffix='mine0001', summary={'success': 1})
    put_task(table, mine, 'extract', '2026-03-01', 'success')
    other = put_run(table, 'billing', '2026-03-01', days_ago(0), suffix='other001', summary={'success': 1})
    put_task(table, other, 'extract', '2026-03-01', 'success')
    assert names(runs(date='2026-03-01', pipeline='orders')[1]) == [mine]


def _backfill(started, **fields):
    from dal import backfills_repo

    backfills_repo.put({'execution_name': fields['backfill_id'], 'status': 'completed', 'started_at': iso(started),
                        'finished_at': iso(started), 'target_pipeline': 'orders', 'total_partitions': 2,
                        'completed_partitions': 2, 'partition_keys': json.dumps(['2026-11-01', '2026-11-02']),
                        **fields})


def test_backfills_merge_by_their_start_and_page_through_the_one_cursor(table):
    run = put_run(table, 'orders', TODAY, days_ago(0), suffix='run00001', summary={'success': 1})
    _backfill(days_ago(1), backfill_id='bf-00000001')
    older = put_run(table, 'orders', '2026-11-19', days_ago(3), suffix='run00002', summary={'success': 1})
    status, body = runs(limit=2)
    assert names(body) == [run, 'bf-00000001'] and [r['kind'] for r in body['runs']] == ['execution', 'backfill']
    assert names(runs(limit=2, before=body['next'])[1]) == [older]


def test_a_backfill_filter_keeps_its_own_vocabulary_and_target(table):
    _backfill(days_ago(1), backfill_id='bf-00000001', status='partial')
    _backfill(days_ago(2), backfill_id='bf-00000002', target_pipeline='billing')
    assert names(runs(status='partial')[1]) == ['bf-00000001']
    assert names(runs(pipeline='billing')[1]) == ['bf-00000002']
    assert names(runs(date='2026-11-02')[1]) == ['bf-00000001', 'bf-00000002']


def test_while_the_index_is_built_the_feed_says_so(table, mocker):
    """§5: an upgrade adds run-start-index, and its reads answer "backfilling" for minutes: transient, and said."""
    from dal import executions_repo
    from response import INDEX_BUILDING_MESSAGE

    building = ClientError({'Error': {'Code': 'ValidationException', 'Message':
                                      'Cannot read from backfilling global secondary index: run-start-index'}},
                           'Query')
    query = mocker.patch.object(type(executions_repo), 'table', new_callable=mocker.PropertyMock)
    query.return_value.query.side_effect = building
    status, body = runs()
    assert (status, body['error'], body['code']) == (503, INDEX_BUILDING_MESSAGE, 'INDEX_BUILDING')


def test_any_other_read_failure_fails_the_request(table, mocker):
    from dal import executions_repo

    query = mocker.patch.object(type(executions_repo), 'table', new_callable=mocker.PropertyMock)
    query.return_value.query.side_effect = ClientError({'Error': {'Code': 'ValidationException',
                                                                  'Message': 'something else'}}, 'Query')
    assert runs()[0] == 500
