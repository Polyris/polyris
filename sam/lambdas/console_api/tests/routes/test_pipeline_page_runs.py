"""The pipeline page's reads on moto tables (ADR #137 §5, §8; D89, D100).

`/api/pipeline-executions` reads a pipeline's run rows from run-start-index: its
history newest first, a page at a time, and a range of dates over every start day of
the 90-day window; one date reads its task rows and the run rows of its runs.
`/api/pipeline-status` and `/api/pipeline-dag` find a date's latest run through the
pipeline's task rows of that date, a query: no route scans the tasks table. A 1.3 run
draws the spec version its run row names; a 1.2 run its DAG snapshot. No route asks
Step Functions about a run. Only the tables, the clock and the Step Functions client
are faked (Principle #14).
"""
import json

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from tests.runs_kit import NOW, days_ago, freeze, iso, open_registry, open_tables, put_run, put_task
from tests.specs_kit import deploy_specs, spec, use_specs_table

TODAY = NOW.strftime('%Y-%m-%d')


@pytest.fixture
def world(mocker):
    from routes import pipelines_list

    with mock_aws():
        resource = boto3.resource('dynamodb', region_name='us-east-1')
        table = open_tables(mocker, resource)
        use_specs_table(mocker, resource)
        freeze(mocker, 'routes.pipelines_list', 'routes.pipelines_info')
        registry = open_registry(mocker, resource)
        sfn = mocker.patch.object(pipelines_list, 'sfn')
        yield type('World', (), {'table': table, 'resource': resource, 'registry': registry})
        assert sfn.method_calls == [], "no route asks Step Functions about a run (ADR #137 §5)"


def executions(**query):
    from routes import pipelines_list

    response = pipelines_list.get_pipeline_executions('orders', {'queryStringParameters': query or None})
    return response['statusCode'], json.loads(response['body'])


def ids(body):
    return [e['execution_id'] for e in body['executions']]


# ── /api/pipeline-executions ─────────────────────────────────────────

def test_the_history_is_the_pipelines_run_rows_newest_first_a_page_at_a_time(world):
    made = [put_run(world.table, 'orders', f'2026-11-{21 - i:02d}', days_ago(i * 7), suffix=f'run{i:05d}',
                    summary={'success': 1}, finished=days_ago(i * 7, hour=7)) for i in range(5)]
    put_run(world.table, 'billing', TODAY, days_ago(0), suffix='other001', summary={'success': 1})
    put_task(world.table, 'orders-run-2026-11-22-legacy01', 'extract', TODAY, 'success', started=days_ago(0))
    status, first = executions(page_size='2')
    assert status == 200 and ids(first) == made[:2] and first['next'] == first['executions'][-1]['started_at']
    rest = executions(page_size='10', before=first['next'])[1]
    assert ids(rest) == made[2:] and rest['next'] is None
    assert first['executions'][0] == {
        'execution_id': made[0], 'execution_short': made[0][-20:], 'date': '2026-11-21', 'status': 'success',
        'failed_tasks': 0, 'failed_step': None, 'started_at': iso(days_ago(0)), 'finished_at': iso(days_ago(0, hour=7)),
        'duration_ms': 3600 * 1000, 'triggered_by': 'schedule'}


def test_a_run_with_a_failed_task_is_running_with_the_count_until_every_row_is_final(world):
    put_run(world.table, 'orders', TODAY, days_ago(0), summary={'failed': 1, 'waiting': 3})
    [run] = executions()[1]['executions']
    assert (run['status'], run['failed_tasks'], run['finished_at']) == ('running', 1, None)


def test_a_range_walks_every_start_day_with_a_filter_on_the_logical_date(world):
    """§5: a run can start before its date (the Run dialog's local date) or long after it (a rerun)."""
    early = put_run(world.table, 'orders', '2026-11-02', days_ago(21), suffix='early001', summary={'success': 1})
    rerun = put_run(world.table, 'orders', '2026-11-05', days_ago(0), suffix='rerun001', summary={'failed': 1})
    inside = put_run(world.table, 'orders', '2026-11-10', days_ago(12), suffix='inside01', summary={'success': 1})
    put_run(world.table, 'orders', '2026-10-31', days_ago(22), suffix='before01', summary={'success': 1})
    put_run(world.table, 'orders', '2026-12-01', days_ago(0, hour=7), suffix='after001', summary={'success': 1})
    status, body = executions(start_date='2026-11-01', end_date='2026-11-30')
    assert status == 200 and ids(body) == [rerun, inside, early] and body['next'] is None


def test_a_range_with_a_bad_date_is_refused(world):
    assert executions(start_date='2026-13-01', end_date='2026-11-30')[0] == 400


def test_one_date_reads_its_task_rows_and_the_run_rows_of_its_runs(world):
    run = put_run(world.table, 'orders', '2026-11-20', days_ago(0), suffix='new13001', summary={'success': 2})
    put_task(world.table, run, 'extract', '2026-11-20', 'success', started=days_ago(0))
    legacy = 'orders-run-2026-11-20-legacy01'
    put_task(world.table, legacy, 'extract', '2026-11-20', 'failed', started=days_ago(2), triggered_by='schedule')
    status, body = executions(date='2026-11-20')
    assert ids(body) == [run, legacy]
    assert [(e['status'], e['failed_tasks']) for e in body['executions']] == [('success', 0), ('failed', 1)]


def test_while_the_index_is_built_the_history_and_a_range_say_so(world, mocker):
    from dal import executions_repo
    from response import INDEX_BUILDING_MESSAGE

    table = mocker.patch.object(type(executions_repo), 'table', new_callable=mocker.PropertyMock)
    table.return_value.query.side_effect = ClientError(
        {'Error': {'Code': 'ValidationException', 'Message': 'Cannot read from backfilling global secondary index'}},
        'Query')
    for query in ({}, {'start_date': '2026-11-01', 'end_date': '2026-11-30'}):
        status, body = executions(**query)
        assert (status, body['error'], body['code']) == (503, INDEX_BUILDING_MESSAGE, 'INDEX_BUILDING')


# ── /api/pipeline-status ─────────────────────────────────────────────

def status_of(date=None, pipeline_execution=None):
    from routes import pipelines_list

    query = {k: v for k, v in (('date', date), ('pipeline_execution', pipeline_execution)) if v}
    return json.loads(pipelines_list.get_pipeline_status('orders', {'queryStringParameters': query})['body'])


def test_the_page_shows_the_latest_run_of_its_date_found_by_a_query(world):
    """Start_Run stamps each row when it writes it, so the run that started last is the date's latest."""
    old = put_run(world.table, 'orders', '2026-11-20', days_ago(2), suffix='first001', summary={'failed': 1})
    put_task(world.table, old, 'extract', '2026-11-20', 'failed', started=days_ago(2))
    new = put_run(world.table, 'orders', '2026-11-20', days_ago(0), suffix='second01', summary={'success': 1})
    put_task(world.table, new, 'extract', '2026-11-20', 'success', started=days_ago(0))
    body = status_of(date='2026-11-20')
    assert body['selected_execution'] == new and [t['status'] for t in body['tasks']] == ['success']
    assert status_of(date='2026-11-20', pipeline_execution=old)['tasks'][0]['status'] == 'failed'


def test_a_date_without_a_run_shows_none(world):
    body = status_of(date='2026-11-01')
    assert (body['tasks'], body['selected_execution']) == ([], None)


def test_the_page_keeps_a_row_s_status_as_the_engine_wrote_it(world):
    """No read-time marking against Step Functions: the engine keeps the rows true (ADR #137 §4)."""
    run = put_run(world.table, 'orders', TODAY, days_ago(0), summary={'running': 1})
    put_task(world.table, run, 'extract', TODAY, 'running', started=days_ago(0))
    assert status_of(date=TODAY)['tasks'][0]['status'] == 'running'


# ── /api/pipeline-dag ────────────────────────────────────────────────

def dag_of(**query):
    from routes import pipelines_info

    return json.loads(pipelines_info.get_pipeline_dag('orders', {'queryStringParameters': query or None})['body'])


def test_a_1_3_run_draws_the_spec_version_its_run_row_names(world):
    """§8: the graph the run ran with, though the registry names a newer version."""

    ran = deploy_specs(world.resource, 'orders', [spec('extract'), spec('load', dependencies=['extract'])])
    current = deploy_specs(world.resource, 'orders', [spec('extract')])
    world.registry.put_item(Item={'pipeline_name': 'orders', **current})
    run = put_run(world.table, 'orders', TODAY, days_ago(0), summary={'success': 2},
                  spec_id=ran['spec_id'], task_count=ran['task_count'])
    body = dag_of(pipeline_execution=run)
    assert body['dag_source'] == 'run' and [n['id'] for n in body['nodes']] == ['extract', 'load']
    assert body['edges'] == [{'from': 'extract', 'to': 'load'}]
    assert dag_of()['dag_source'] == 'registry' and [n['id'] for n in dag_of()['nodes']] == ['extract']


def test_a_1_2_run_draws_its_dag_snapshot_until_1_4(world):
    """D89: a run from before 1.3 has no run row; its snapshot is read, with each countdown from its rows."""
    legacy = 'orders-run-2026-11-20-legacy01'
    world.table.put_item(Item={'execution_name': f'dag_snapshot::{legacy}', 'pipeline_execution': legacy,
                               'dag': json.dumps({'nodes': [{'id': 'a'}, {'id': 'b'}], 'edges': []})})
    put_task(world.table, legacy, 'b', '2026-11-20', 'success', wait_before='30')
    body = dag_of(pipeline_execution=legacy)
    assert body['dag_source'] == 'snapshot' and body['nodes'] == [{'id': 'a'}, {'id': 'b', 'wait_before': 30}]


def test_without_specs_the_graph_is_inferred_from_the_latest_run_of_the_date(world):
    run = put_run(world.table, 'orders', TODAY, days_ago(0))
    put_task(world.table, run, 'extract', TODAY, 'success', started=days_ago(0))
    put_task(world.table, run, 'load', TODAY, 'waiting', started=days_ago(0), dependencies='["extract"]')
    body = dag_of(date=TODAY)
    assert body['dag_source'] == 'inferred' and sorted(n['id'] for n in body['nodes']) == ['extract', 'load']
    assert body['edges'] == [{'from': 'extract', 'to': 'load'}]


def test_a_waiting_task_shows_why_it_waits(world):
    """ADR #137 §1: the reasons the dispatcher recorded and since when, its deadline, and what its trigger rule
    makes of its upstreams, read from the rows of 1.3 (their readiness, not 1.2's fields)."""
    from polyris.coordination import task_spec
    run = put_run(world.table, 'orders', '2026-11-20', days_ago(0), summary={'running': 1, 'waiting': 1})
    readiness = task_spec.canonical_json({'trigger_rule': 'all_success', 'dependencies': ['extract'],
                                          'orchestration_timeout': 43200, 'wait_before': 60})
    put_task(world.table, run, 'extract', '2026-11-20', 'running', started=days_ago(0),
             readiness=task_spec.canonical_json({'trigger_rule': 'all_success', 'dependencies': []}))
    put_task(world.table, run, 'load', '2026-11-20', 'waiting', started=days_ago(0), readiness=readiness,
             reasons=['waits for extract'], reasons_at='2026-11-22T06:00:00.000Z', attention='waiting#x',
             attention_at='2026-11-22T18:00:00.000Z')
    load = next(t for t in status_of(date='2026-11-20')['tasks'] if t['task_name'] == 'load')
    assert (load['dependencies'], load['trigger_rule'], load['wait_before']) == (['extract'], 'all_success', 60)
    assert (load['reasons'], load['reasons_at'], load['deadline'], load['countdown_end']) == (
        ['waits for extract'], '2026-11-22T06:00:00.000Z', '2026-11-22T18:00:00.000Z', None)
    assert load['dependency_verdict']['verdict'] == 'wait'
    assert load['dependency_verdict']['upstreams'] == [{'name': 'extract', 'status': 'running', 'counts_as': 'pending'}]


def test_a_task_counting_down_its_wait_before_shows_when_it_starts_and_no_deadline(world):
    """Review of TR-6d, P-3: the row falls due when its countdown ends (deadlines.due_time), when it starts."""
    from polyris.coordination import task_spec
    run = put_run(world.table, 'orders', '2026-11-20', days_ago(0), summary={'success': 1, 'waiting': 1})
    put_task(world.table, run, 'extract', '2026-11-20', 'success', started=days_ago(0),
             readiness=task_spec.canonical_json({'trigger_rule': 'all_success', 'dependencies': []}))
    put_task(world.table, run, 'load', '2026-11-20', 'waiting', started=days_ago(0),
             readiness=task_spec.canonical_json({'trigger_rule': 'all_success', 'dependencies': ['extract'],
                                                 'wait_before': 600}),
             reasons=['counting down its wait_before'], reasons_at='2026-11-22T06:00:00.000Z',
             countdown_end='2026-11-22T06:10:00.000Z', attention='waiting#x', attention_at='2026-11-22T06:10:00.000Z')
    load = next(t for t in status_of(date='2026-11-20')['tasks'] if t['task_name'] == 'load')
    assert (load['countdown_end'], load['deadline']) == ('2026-11-22T06:10:00.000Z', None)


def test_the_history_names_a_failed_direct_step(world):
    """Review of TR-6d, P-8: a run that failed by a direct step alone showed failed with no task failed."""
    from polyris.coordination import run_row
    put_run(world.table, 'orders', TODAY, days_ago(0), summary={'success': 2}, finished=days_ago(0, hour=7),
            step_failure={run_row.STEP_MARK_STEP: 'notify', run_row.STEP_MARK_ERROR: 'AccessDenied'})
    status, body = executions()
    [run] = body['executions']
    assert (status, run['status'], run['failed_tasks']) == (200, 'failed', 0)
    assert run['failed_step'] == {'step': 'notify', 'error': 'AccessDenied'}
