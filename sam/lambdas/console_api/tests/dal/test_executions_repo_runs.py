"""
Unit tests for dal.executions_repo.ExecutionsRepo.query_runs_by_pipeline.

This is the windowless read path: instead of looping a window of days against
date-pipeline-index, it asks pipeline-date-index for "this pipeline's runs,
newest first" and pages with an opaque date cursor.

The behaviour that matters (and is easy to get wrong):
  - it must cut only on a *date boundary*, so an execution's task rows are never
    split across pages (the caller aggregates them into one run);
  - it must keep reading while only one date is buffered, however busy that date is;
  - it must stop as soon as the data runs out, reporting no cursor.

Pattern follows test_backfills_repo.py — mock the .table property. No moto, no AWS.
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
# Exhaustion (no cursor)
# ──────────────────────────────────────────────────────────────────────────────

def test_returns_all_rows_and_no_cursor_when_data_runs_out(mocker):
    """A single page with no LastEvaluatedKey → everything, cursor None."""
    table = _table(mocker, [{'Items': [_row('2026-07-14', 'e1'), _row('2026-07-13', 'e2')]}])
    repo = _make_repo_with_table(mocker, table)

    items, cursor = repo.query_runs_by_pipeline('p', min_runs=15)

    assert len(items) == 2
    assert cursor is None


def test_keeps_paging_until_exhausted_when_min_runs_not_reached(mocker):
    """Fewer runs than asked for → follow LastEvaluatedKey to the end."""
    table = _table(mocker, [
        {'Items': [_row('2026-07-14', 'e1')], 'LastEvaluatedKey': {'k': 1}},
        {'Items': [_row('2026-07-13', 'e2')]},
    ])
    repo = _make_repo_with_table(mocker, table)

    items, cursor = repo.query_runs_by_pipeline('p', min_runs=15)

    assert [i['pipeline_execution'] for i in items] == ['e1', 'e2']
    assert cursor is None
    assert table.query.call_count == 2


# ──────────────────────────────────────────────────────────────────────────────
# Cutting on a date boundary
# ──────────────────────────────────────────────────────────────────────────────

def test_cuts_on_date_boundary_and_drops_the_partial_oldest_date(mocker):
    """Once enough runs are buffered, cut at the oldest date and hand it back
    as the cursor — its rows are withheld because they may be half-read."""
    table = _table(mocker, [
        {'Items': [_row('2026-07-14', 'e1'), _row('2026-07-13', 'e2')],
         'LastEvaluatedKey': {'k': 1}},
    ])
    repo = _make_repo_with_table(mocker, table)

    items, cursor = repo.query_runs_by_pipeline('p', min_runs=2)

    # 2026-07-13 is the cut date: withheld here, re-read on the next page.
    assert [i['pipeline_execution'] for i in items] == ['e1']
    assert cursor == '2026-07-13'


def test_does_not_cut_while_only_one_date_is_buffered(mocker):
    """A busy single date must be read whole, even past min_runs — otherwise
    the cut date would be the only date and the page would be empty."""
    table = _table(mocker, [
        {'Items': [_row('2026-07-14', 'e1'), _row('2026-07-14', 'e2')],
         'LastEvaluatedKey': {'k': 1}},
        {'Items': [_row('2026-07-14', 'e3')], 'LastEvaluatedKey': {'k': 2}},
        {'Items': [_row('2026-07-13', 'e4')], 'LastEvaluatedKey': {'k': 3}},
    ])
    repo = _make_repo_with_table(mocker, table)

    items, cursor = repo.query_runs_by_pipeline('p', min_runs=2)

    # Kept reading through the busy date, then cut once an older date appeared.
    assert [i['pipeline_execution'] for i in items] == ['e1', 'e2', 'e3']
    assert cursor == '2026-07-13'
    assert table.query.call_count == 3


def test_tasks_of_one_run_are_never_split_across_pages(mocker):
    """All rows of a returned run come back together (the point of the boundary)."""
    table = _table(mocker, [
        {'Items': [_row('2026-07-14', 'e1', 'extract'), _row('2026-07-14', 'e1', 'load'),
                   _row('2026-07-13', 'e2', 'extract')],
         'LastEvaluatedKey': {'k': 1}},
    ])
    repo = _make_repo_with_table(mocker, table)

    items, _ = repo.query_runs_by_pipeline('p', min_runs=1)

    e1_tasks = [i['task_name'] for i in items if i['pipeline_execution'] == 'e1']
    assert sorted(e1_tasks) == ['extract', 'load']


# ──────────────────────────────────────────────────────────────────────────────
# Query construction
# ──────────────────────────────────────────────────────────────────────────────

def test_queries_the_inverted_index_newest_first(mocker):
    """Uses pipeline-date-index descending — that is what removes the day loop."""
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)

    repo.query_runs_by_pipeline('my-pipeline')

    params = table.query.call_args.kwargs
    assert params['IndexName'] == 'pipeline-date-index'
    assert params['ScanIndexForward'] is False


def test_cursor_is_inclusive_so_the_cut_date_is_re_read(mocker):
    """before_date narrows the key condition with `date <= cursor` (inclusive),
    so the withheld cut date is re-read rather than skipped."""
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)

    repo.query_runs_by_pipeline('p', before_date='2026-07-13')

    expr = table.query.call_args.kwargs['KeyConditionExpression'].get_expression()
    assert expr['operator'] == 'AND'
    sort_cond = expr['values'][1].get_expression()
    assert sort_cond['operator'] == '<='          # inclusive, not '<'
    assert sort_cond['values'][1] == '2026-07-13'


def test_no_cursor_means_no_sort_key_condition(mocker):
    """The first page is just `pipeline_name = X` — no date bound at all."""
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)

    repo.query_runs_by_pipeline('p')

    expr = table.query.call_args.kwargs['KeyConditionExpression'].get_expression()
    assert expr['operator'] == '='                # only the partition key
    assert expr['values'][1] == 'p'


def test_projection_and_expression_names_are_forwarded(mocker):
    """Callers project a narrow attribute set; reserved words need aliases."""
    table = _table(mocker, [{'Items': []}])
    repo = _make_repo_with_table(mocker, table)

    repo.query_runs_by_pipeline(
        'p', projection='pipeline_execution, #s', expr_names={'#s': 'status'},
    )

    params = table.query.call_args.kwargs
    assert params['ProjectionExpression'] == 'pipeline_execution, #s'
    assert params['ExpressionAttributeNames'] == {'#s': 'status'}


def test_rows_missing_keys_do_not_count_as_runs(mocker):
    """Malformed rows must not satisfy min_runs (they'd cut the page early)."""
    table = _table(mocker, [
        {'Items': [{'task_name': 'orphan'}, _row('2026-07-14', 'e1')],
         'LastEvaluatedKey': {'k': 1}},
        {'Items': [_row('2026-07-13', 'e2')]},
    ])
    repo = _make_repo_with_table(mocker, table)

    items, cursor = repo.query_runs_by_pipeline('p', min_runs=2)

    assert cursor is None
    assert len(items) == 3


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
    assert params['ProjectionExpression'] == '#s'
    assert params['ExpressionAttributeNames'] == {'#s': 'status'}
    assert params['FilterExpression'].get_expression()['values'][1] == 'failed'
    assert params['KeyConditionExpression'].get_expression()['operator'] == 'AND'
