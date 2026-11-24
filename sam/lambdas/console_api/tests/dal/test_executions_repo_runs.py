"""
Unit tests for dal.executions_repo.ExecutionsRepo.query_runs_by_date.

This is the windowless read path: instead of looping a window of days against
date-pipeline-index, it asks pipeline-date-index for "this pipeline's runs,
newest first" and pages with an opaque date cursor.

The behaviour that matters (and is easy to get wrong):
  - it must cut only on a *date boundary*, so an execution's task rows are never
    split across pages (the caller aggregates them into one run);
  - it must keep reading while only one date is buffered, however busy that date is;
  - it must stop as soon as the data runs out, reporting no cursor.

The .table property is mocked with finite query pages. No moto, no AWS.
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _make_repo_with_table(mocker, table_mock):
    """Build an ExecutionsRepo whose .table property returns our mock."""
    from dal.executions_repo import ExecutionsRepo
    repo = ExecutionsRepo()
    mocker.patch.object(
        ExecutionsRepo, 'table',
        new_callable=mocker.PropertyMock,
        return_value=table_mock,
    )
    return repo


def _row(date, execution, task='t1'):
    return {
        'date': date,
        'pipeline_execution': execution,
        'task_name': task,
        'status': 'success',
    }


def _table(mocker, pages):
    """Mock table whose .query returns each page in turn."""
    table = mocker.Mock()
    table.query = mocker.Mock(side_effect=pages)
    return table


# ──────────────────────────────────────────────────────────────────────────────
# query_runs_by_date — the mirror read: one date, whole pipelines only.
#
# date-pipeline-index is ordered by pipeline_name, so a row-count cut lands
# mid-pipeline and splits a run's task set. /api/runs derives a run's status from the
# rows it got (ADR #112), so that does not drop a run — it renders a wrong one.
# ──────────────────────────────────────────────────────────────────────────────

def _prow(pipeline, execution, task='t1', status='success'):
    return {'date': '2026-07-16', 'pipeline_name': pipeline,
            'pipeline_execution': execution, 'task_name': task, 'status': status}


def test_a_date_that_fits_one_page_comes_back_whole(mocker):
    """Nothing is half-read, so there is nothing to cut — min_rows is irrelevant."""
    table = _table(mocker, [{'Items': [_prow('a', 'e1'), _prow('b', 'e2')]}])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=1)

    assert len(rows) == 2


def test_drops_the_pipeline_the_cut_landed_inside(mocker):
    """`b` may be half-read, so its runs would derive their status from a subset.
    Drop it whole; it is not this day's data any more."""
    table = _table(mocker, [
        {'Items': [_prow('a', 'e1'), _prow('a', 'e2'), _prow('b', 'e3')],
         'LastEvaluatedKey': {'k': 1}},
    ])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=2)

    assert {r['pipeline_name'] for r in rows} == {'a'}


def test_never_splits_a_runs_task_set(mocker):
    """The property that matters: every returned run brings all of its rows."""
    table = _table(mocker, [
        {'Items': [_prow('a', 'e1', 'extract'), _prow('a', 'e1', 'load'),
                   _prow('b', 'e2', 'extract')],
         'LastEvaluatedKey': {'k': 1}},
    ])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=1)

    assert sorted(r['task_name'] for r in rows if r['pipeline_execution'] == 'e1') == \
        ['extract', 'load']
    assert not any(r['pipeline_execution'] == 'e2' for r in rows)


def test_does_not_cut_while_only_one_pipeline_is_buffered(mocker):
    """A single busy pipeline must never be the one dropped — its day would vanish
    whole. Keep reading until a second pipeline appears."""
    table = _table(mocker, [
        {'Items': [_prow('a', 'e1'), _prow('a', 'e2')], 'LastEvaluatedKey': {'k': 1}},
        {'Items': [_prow('a', 'e3')], 'LastEvaluatedKey': {'k': 2}},
        {'Items': [_prow('b', 'e4')], 'LastEvaluatedKey': {'k': 3}},
    ])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=1)

    assert [r['pipeline_execution'] for r in rows] == ['e1', 'e2', 'e3']
    assert table.query.call_count == 3


def test_keeps_reading_until_min_rows_is_covered(mocker):
    """With a status filter most rows are dropped server-side, so a page can come back
    nearly empty — that is what min_rows is for. It cuts as soon as the budget is
    covered *and* a boundary is available, not a page later."""
    table = _table(mocker, [
        {'Items': [_prow('a', 'e1')], 'LastEvaluatedKey': {'k': 1}},
        {'Items': [_prow('b', 'e2')], 'LastEvaluatedKey': {'k': 2}},
        {'Items': [_prow('c', 'e3')], 'LastEvaluatedKey': {'k': 3}},
    ])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=3)

    assert table.query.call_count == 3          # 1 row, 2 rows, 3 rows -> cut at `c`
    assert {r['pipeline_name'] for r in rows} == {'a', 'b'}


def test_cuts_as_soon_as_the_budget_and_a_boundary_are_both_there(mocker):
    table = _table(mocker, [
        {'Items': [_prow('a', 'e1')], 'LastEvaluatedKey': {'k': 1}},
        {'Items': [_prow('b', 'e2')], 'LastEvaluatedKey': {'k': 2}},
        {'Items': [_prow('c', 'e3')], 'LastEvaluatedKey': {'k': 3}},
    ])
    repo = _make_repo_with_table(mocker, table)

    rows = repo.query_runs_by_date('2026-07-16', min_rows=2)

    assert table.query.call_count == 2          # `b` covers the budget and is the cut
    assert {r['pipeline_name'] for r in rows} == {'a'}


def test_queries_the_date_index(mocker):
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)

    repo.query_runs_by_date('2026-07-16', min_rows=10)

    params = table.query.call_args.kwargs
    assert params['IndexName'] == 'date-pipeline-index'
    expr = params['KeyConditionExpression'].get_expression()
    assert expr['operator'] == '='
    assert expr['values'][1] == '2026-07-16'


def test_forwards_projection_filter_and_key_condition(mocker):
    from boto3.dynamodb.conditions import Attr, Key
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)
    key = Key('date').eq('2026-07-16') & Key('pipeline_name').eq('sales')

    repo.query_runs_by_date('2026-07-16', min_rows=10, projection='#s',
                            expr_names={'#s': 'status'}, filter_expr=Attr('status').eq('failed'),
                            key_condition=key)

    params = table.query.call_args.kwargs
    assert params['ProjectionExpression'] == '#s, execution_name, pipeline_name'
    assert params['ExpressionAttributeNames'] == {'#s': 'status'}
    assert params['FilterExpression'].get_expression()['values'][1] == 'failed'
    assert params['KeyConditionExpression'].get_expression()['operator'] == 'AND'
