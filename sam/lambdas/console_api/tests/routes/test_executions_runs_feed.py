"""
Unit tests for the unified Run/Activity feed in routes.executions.get_all_runs
(ADR #95): Backfills merged into /api/runs as first-class ``kind='backfill'``
rows alongside ``kind='execution'`` rows.

Pattern follows test_backfill.py — pytest-mock (ADR #26), patch the repo methods
get_all_runs calls (its external data boundary). The merge/filter/sort logic and
should_skip_token_row run for real (CLAUDE.md #13/#14: pin the integration
contract, mock only at the boundary).
"""

import json
from datetime import datetime, timezone

import pytest
from botocore.exceptions import ClientError

from constants import Limits
import feed


TODAY = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_today(mocker):
    """Pin the day the fan-out walks back from. Without this, any test that asserts
    *which* dates were queried rots silently the moment its hardcoded cursor falls
    out of the SLA window. (CLAUDE.md #14 — time is a boundary, so mock it there.)"""
    mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: TODAY))


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _event(date=None, status=None, pipeline=None, before=None, limit=None):
    qs = {}
    if date:
        qs['date'] = date
    if status:
        qs['status'] = status
    if pipeline:
        qs['pipeline'] = pipeline
    if before:
        qs['before'] = before
    if limit:
        qs['limit'] = str(limit)
    return {'queryStringParameters': qs or None}


def _exec_row(pe='p-2024-01-15-abc', name='extract-2024-01-15-abc',
              pipeline='test-pipeline', status='running',
              started='2024-01-15T10:00:00Z', date='2024-01-15'):
    return {
        'execution_name': name,
        'pipeline_execution': pe,
        'pipeline_execution_short': pe[-8:],
        'pipeline_name': pipeline,
        'status': status,
        'date': date,
        'started_at': started,
        'finished_at': '',
    }


def _backfill_record(bf_id='bf-abc123', target='test-pipeline', status='completed',
                     started='2024-01-15T12:00:00Z', finished='2024-01-15T12:30:00Z',
                     keys=None, total=10, completed=10, failed=0, skipped=0,
                     cascade='auto', granularity='daily', started_by='alice'):
    return {
        'execution_name': bf_id,
        'backfill_id': bf_id,
        'record_type': 'backfill',
        'pipeline_name': '_polyris_bulk_backfill',
        'target_pipeline': target,
        'status': status,
        'started_at': started,
        'finished_at': finished,
        'started_by': started_by,
        'partition_keys': json.dumps(keys if keys is not None
                                     else ['2024-01-10', '2024-01-11', '2024-01-12']),
        'total_partitions': total,
        'completed_partitions': completed,
        'failed_partitions': failed,
        'skipped_partitions': skipped,
        'cascade': cascade,
        'granularity': granularity,
    }


def _call(mocker, exec_rows, backfill_records, event):
    """Patch the two repos get_all_runs depends on, then call it."""
    from routes import executions
    mocker.patch.object(executions.executions_repo, 'query_runs_by_date',
                         return_value=list(exec_rows))
    mocker.patch.object(executions.backfills_repo, 'list_recent',
                        return_value=list(backfill_records))
    resp = executions.get_all_runs(event)
    return resp, json.loads(resp['body'])


# ──────────────────────────────────────────────────────────────────────────────
# kind discriminator (ADR #95 decision 1)
# ──────────────────────────────────────────────────────────────────────────────

class TestKindDiscriminator:
    def test_executions_tagged_kind_execution(self, mocker):
        resp, body = _call(mocker, [_exec_row()], [], _event(date='2024-01-15'))
        assert resp['statusCode'] == 200
        execs = [r for r in body['runs'] if r['kind'] == 'execution']
        assert len(execs) == 1
        assert execs[0]['pipeline_execution'] == 'p-2024-01-15-abc'

    def test_backfills_tagged_kind_backfill_with_expected_fields(self, mocker):
        resp, body = _call(mocker, [], [_backfill_record()], _event(date='2024-01-11'))
        bfs = [r for r in body['runs'] if r['kind'] == 'backfill']
        assert len(bfs) == 1
        row = bfs[0]
        assert row['id'] == 'bf-abc123'
        assert row['backfill_id'] == 'bf-abc123'
        assert row['pipeline_name'] == 'test-pipeline'
        assert row['status'] == 'completed'
        assert row['total_partitions'] == 10
        assert row['completed_partitions'] == 10
        assert row['downstream'] == 'auto'
        assert row['granularity'] == 'daily'
        # finished - started == 30 min
        assert row['duration_ms'] == 30 * 60 * 1000

    def test_every_row_has_a_kind(self, mocker):
        resp, body = _call(mocker, [_exec_row()], [_backfill_record(keys=['2024-01-15'])],
                           _event(date='2024-01-15'))
        assert all('kind' in r for r in body['runs'])
        assert {r['kind'] for r in body['runs']} == {'execution', 'backfill'}


# ──────────────────────────────────────────────────────────────────────────────
# No double-count + no internal/sentinel leakage (CLAUDE.md #13, ADR #38)
# ──────────────────────────────────────────────────────────────────────────────

class TestNoLeakage:
    def test_backfill_sentinel_and_internal_rows_never_appear_as_executions(self, mocker):
        # query_by_date returns a real exec row, a backfill-sentinel row, and an
        # internal _notify_warn_ row. Only the exec row may become an execution.
        sentinel_in_tokens = {
            'execution_name': 'bf-leak',
            'pipeline_execution': 'bf-leak',
            'pipeline_name': '_polyris_bulk_backfill',
            'record_type': 'backfill',
            'status': 'running',
            'date': '2024-01-15',
            'started_at': '2024-01-15T09:00:00Z',
        }
        notify_warn = {
            'execution_name': '_notify_warn_extract-2024-01-15-abc',
            'pipeline_execution': 'p-2024-01-15-abc',
            'pipeline_name': 'test-pipeline',
            'status': 'failed',
            'date': '2024-01-15',
            'started_at': '2024-01-15T09:30:00Z',
        }
        resp, body = _call(
            mocker,
            [_exec_row(), sentinel_in_tokens, notify_warn],
            [_backfill_record(bf_id='bf-real', keys=['2024-01-15'])],
            _event(date='2024-01-15'),
        )
        execs = [r for r in body['runs'] if r['kind'] == 'execution']
        bfs = [r for r in body['runs'] if r['kind'] == 'backfill']
        # exactly one execution (the real one); sentinel + notify_warn filtered
        assert len(execs) == 1
        assert execs[0]['pipeline_execution'] == 'p-2024-01-15-abc'
        assert all(r['pipeline_name'] != '_polyris_bulk_backfill' for r in execs)
        # the backfill appears exactly once, sourced from list_recent
        assert len(bfs) == 1
        assert bfs[0]['backfill_id'] == 'bf-real'


# ──────────────────────────────────────────────────────────────────────────────
# Filters (ADR #95 decisions 2-4)
# ──────────────────────────────────────────────────────────────────────────────

class TestFilters:
    def test_status_filter_matches_backfill_vocabulary(self, mocker):
        # ?status=completed: backfill (completed) in, running execution out.
        resp, body = _call(
            mocker,
            [_exec_row(status='running')],
            [_backfill_record(status='completed', keys=['2024-01-15'])],
            _event(date='2024-01-15', status='completed'),
        )
        kinds = {r['kind'] for r in body['runs']}
        assert kinds == {'backfill'}

    def test_pipeline_filter_matches_backfill_target(self, mocker):
        resp, body = _call(
            mocker,
            [],
            [
                _backfill_record(bf_id='bf-match', target='wanted', keys=['2024-01-15']),
                _backfill_record(bf_id='bf-other', target='other', keys=['2024-01-15']),
            ],
            _event(date='2024-01-15', pipeline='wanted'),
        )
        ids = {r['backfill_id'] for r in body['runs'] if r['kind'] == 'backfill'}
        assert ids == {'bf-match'}

    def test_date_filter_includes_backfill_in_partition_range(self, mocker):
        # range covers 2024-01-10..2024-01-12
        _, in_range = _call(mocker, [], [_backfill_record()], _event(date='2024-01-11'))
        assert any(r['kind'] == 'backfill' for r in in_range['runs'])

    def test_date_filter_excludes_backfill_outside_partition_range(self, mocker):
        _, out_of_range = _call(mocker, [], [_backfill_record()], _event(date='2024-02-01'))
        assert not any(r['kind'] == 'backfill' for r in out_of_range['runs'])


# ──────────────────────────────────────────────────────────────────────────────
# Sort + graceful degradation (ADR #95 decision 5)
# ──────────────────────────────────────────────────────────────────────────────

class TestSortAndDegrade:
    def test_merged_feed_sorted_by_started_at_desc(self, mocker):
        # exec at 10:00, backfill at 12:00 → backfill first
        resp, body = _call(
            mocker,
            [_exec_row(started='2024-01-15T10:00:00Z')],
            [_backfill_record(started='2024-01-15T12:00:00Z', keys=['2024-01-15'])],
            _event(date='2024-01-15'),
        )
        assert body['runs'][0]['kind'] == 'backfill'
        assert body['runs'][1]['kind'] == 'execution'

    def test_list_recent_error_degrades_gracefully(self, mocker):
        from routes import executions
        mocker.patch.object(executions.executions_repo, 'query_runs_by_date',
                            return_value=[_exec_row()])
        mocker.patch.object(
            executions.backfills_repo, 'list_recent',
            side_effect=ClientError({'Error': {'Code': 'X', 'Message': 'boom'}}, 'Scan'),
        )
        resp = executions.get_all_runs(_event(date='2024-01-15'))
        body = json.loads(resp['body'])
        # executions still returned, no 500
        assert resp['statusCode'] == 200
        assert any(r['kind'] == 'execution' for r in body['runs'])
        assert not any(r['kind'] == 'backfill' for r in body['runs'])


# ──────────────────────────────────────────────────────────────────────────────
# Pipeline filter → windowless index path (ADR #108)
# ──────────────────────────────────────────────────────────────────────────────

def test_pipeline_filter_uses_the_index_not_the_day_loop(mocker):
    """Filtering History to one pipeline must hit pipeline-date-index once, not
    walk SLA_DAYS of dates — that is what removes the window from this view."""
    from routes import executions

    by_pipeline = mocker.patch.object(
        executions.executions_repo, 'query_runs_by_pipeline',
        return_value=([_exec_row(name='extract-2024-01-15-abc')], None),
    )
    by_date = mocker.patch.object(executions.executions_repo, 'query_runs_by_date')
    mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

    resp = executions.get_all_runs(_event(pipeline='my-pipeline'))

    assert resp['statusCode'] == 200
    by_pipeline.assert_called_once()
    assert by_pipeline.call_args.args[0] == 'my-pipeline'
    by_date.assert_not_called()          # no day loop


def test_explicit_date_still_wins_over_the_index(mocker):
    """An explicit date is already bounded — keep the single-date GSI query."""
    from routes import executions

    by_pipeline = mocker.patch.object(executions.executions_repo, 'query_runs_by_pipeline')
    by_date = mocker.patch.object(
        executions.executions_repo, 'query_runs_by_date', return_value=[_exec_row()],
    )
    mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

    executions.get_all_runs(_event(date='2024-01-15', pipeline='my-pipeline'))

    by_date.assert_called_once()
    by_pipeline.assert_not_called()


def test_cross_pipeline_feed_still_fans_out_over_dates(mocker):
    """With no pipeline to hash on, `date` is the only shard key — the fan-out
    stays deliberately (ADR #108), so the index must not be used here."""
    from routes import executions

    by_pipeline = mocker.patch.object(executions.executions_repo, 'query_runs_by_pipeline')
    by_date = mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=[])
    mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

    executions.get_all_runs(_event())

    by_pipeline.assert_not_called()
    assert by_date.call_count > 1        # one query per day of the window


# ──────────────────────────────────────────────────────────────────────────────
# Cursor paging — the unified feed pages on started_at, both kinds through one
# cursor (feed.py). Replaces the silent `all_runs[:limit]` slice.
# ──────────────────────────────────────────────────────────────────────────────

def _runs_of(body):
    return [r.get('pipeline_execution') or r.get('backfill_id') for r in body['runs']]


class TestPaging:
    def test_full_page_hands_back_a_cursor(self, mocker):
        rows = [_exec_row(pe=f'p-{i}', name=f'extract-2024-01-15-{i}',
                          started=f'2024-01-15T10:0{i}:00Z') for i in range(5)]
        _, body = _call(mocker, rows, [], _event(date='2024-01-15', limit=3))

        assert body['count'] == 3
        assert _runs_of(body) == ['p-4', 'p-3', 'p-2']
        assert body['next'] == '2024-01-15T10:02:00Z'

    def test_no_cursor_when_nothing_older_exists(self, mocker):
        """The honest "that is all" — the whole reason the slice had to go."""
        _, body = _call(mocker, [_exec_row()], [], _event(date='2024-01-15', limit=3))
        assert body['next'] is None

    def test_cursor_serves_the_next_page_without_repeats(self, mocker):
        rows = [_exec_row(pe=f'p-{i}', name=f'extract-2024-01-15-{i}',
                          started=f'2024-01-15T10:0{i}:00Z') for i in range(5)]
        _, first = _call(mocker, rows, [], _event(date='2024-01-15', limit=3))
        _, second = _call(mocker, rows, [], _event(date='2024-01-15', limit=3,
                                                   before=first['next']))

        assert _runs_of(second) == ['p-1', 'p-0']
        assert second['next'] is None
        assert not set(_runs_of(first)) & set(_runs_of(second))

    def test_both_kinds_page_through_the_one_cursor(self, mocker):
        """Runs and Backfills interleave by started_at, so one cursor covers both —
        no composite key needed (ADR #95 gives both kinds a started_at)."""
        rows = [_exec_row(pe='p-1', started='2024-01-15T10:00:00Z'),
                _exec_row(pe='p-2', name='load-2024-01-15-x', started='2024-01-15T12:00:00Z')]
        bfs = [_backfill_record(bf_id='bf-1', started='2024-01-15T11:00:00Z', keys=['2024-01-15']),
               _backfill_record(bf_id='bf-2', started='2024-01-15T13:00:00Z', keys=['2024-01-15'])]

        _, first = _call(mocker, rows, bfs, _event(date='2024-01-15', limit=2))
        assert _runs_of(first) == ['bf-2', 'p-2']
        assert first['next'] == '2024-01-15T12:00:00Z'

        _, second = _call(mocker, rows, bfs, _event(date='2024-01-15', limit=2,
                                                    before=first['next']))
        assert _runs_of(second) == ['bf-1', 'p-1']
        assert second['next'] is None

    def test_backfills_page_through_the_repo_not_after_its_limit(self, mocker):
        """The trap: list_recent sorts newest-first and slices, so filtering the
        cursor *afterwards* would hand back an empty set for every page but the
        first — Backfills would quietly drop out of the feed as soon as you paged."""
        from routes import executions
        mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=[])
        list_recent = mocker.patch.object(executions.backfills_repo, 'list_recent',
                                          return_value=[])

        executions.get_all_runs(_event(date='2024-01-15', before='2024-01-15T10:00:00Z'))

        assert list_recent.call_args.kwargs['before'] == '2024-01-15T10:00:00Z'

    def test_backfills_alone_can_still_page(self, mocker):
        """Regression: list_recent was asked for exactly `limit`, so a feed whose page
        is filled by Backfills alone (a backfill-only status filter, say) could never
        exceed the page size — it reported itself exhausted with older Backfills
        behind it. Walk the whole feed and demand every one of them."""
        from routes import executions
        all_bf = [_backfill_record(bf_id=f'bf-{i:02d}', status='completed',
                                   started=f'2024-01-15T{23 - i:02d}:00:00Z',
                                   keys=['2024-01-15']) for i in range(12)]

        def fake_list_recent(limit=50, before=None):
            items = [b for b in all_bf if not before or b['started_at'] < before]
            items.sort(key=lambda x: x['started_at'], reverse=True)
            return items[:limit]

        mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=[])
        mocker.patch.object(executions.backfills_repo, 'list_recent',
                            side_effect=fake_list_recent)

        seen, cursor = [], None
        for _ in range(20):
            resp = executions.get_all_runs(_event(date='2024-01-15', limit=5, before=cursor))
            body = json.loads(resp['body'])
            seen.extend(r['backfill_id'] for r in body['runs'])
            cursor = body['next']
            if cursor is None:
                break
        else:
            raise AssertionError('paging never terminated')

        assert len(seen) == len(set(seen)) == 12

    def test_no_cursor_asks_the_repo_for_no_cursor(self, mocker):
        from routes import executions
        mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=[])
        list_recent = mocker.patch.object(executions.backfills_repo, 'list_recent',
                                          return_value=[])

        executions.get_all_runs(_event(date='2024-01-15'))

        assert list_recent.call_args.kwargs['before'] is None

    def test_cursor_starts_the_fan_out_at_its_own_date(self, mocker, frozen_today):
        """Paging deeper must not re-query days that are already served — and so costs
        fewer queries, not more."""
        from routes import executions
        by_date = mocker.patch.object(executions.executions_repo, 'query_runs_by_date',
                                      return_value=[])
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

        executions.get_all_runs(_event(before='2026-07-14T10:00:00Z'))

        queried = [c.args[0] for c in by_date.call_args_list]
        assert queried[0] == '2026-07-14'           # the cursor's date, not today
        assert '2026-07-15' not in queried          # already served
        assert len(queried) == Limits.SLA_DAYS - 2

    def test_a_cursor_older_than_the_window_queries_nothing(self, mocker, frozen_today):
        """The walk never leaves the window, whatever cursor a client sends back."""
        from routes import executions
        by_date = mocker.patch.object(executions.executions_repo, 'query_runs_by_date',
                                      return_value=[])
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

        resp = executions.get_all_runs(_event(before='2024-01-15T10:00:00Z'))

        assert by_date.call_count == 0
        assert json.loads(resp['body'])['next'] is None

    def test_a_full_page_is_not_mistaken_for_the_end_of_the_feed(self, mocker, frozen_today):
        """Regression: the index read stopped at exactly `limit` runs, so a full page
        was indistinguishable from an exhausted feed — `next` came back None and every
        older run was stranded. The read must overshoot the page by one."""
        from routes import executions
        rows = [_exec_row(pe=f'p-{i}', name=f'extract-2026-07-16-{i}',
                          started=f'2026-07-16T1{i}:00:00Z') for i in range(5)]
        mocker.patch.object(
            executions.executions_repo, 'query_runs_by_pipeline',
            side_effect=[(rows, '2026-07-15'),
                         ([_exec_row(pe='p-old', name='extract-2026-07-15-x',
                                     started='2026-07-15T10:00:00Z', date='2026-07-15')], None)],
        )
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

        _, body = None, json.loads(
            executions.get_all_runs(_event(pipeline='sales', limit=5))['body'])

        assert body['count'] == 5
        assert body['next'] is not None      # the older run is reachable

    def test_cursor_seeds_the_index_read_for_a_pipeline(self, mocker):
        from routes import executions
        by_pipeline = mocker.patch.object(executions.executions_repo,
                                          'query_runs_by_pipeline', return_value=([], None))
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

        executions.get_all_runs(_event(pipeline='my-pipeline', before='2024-01-15T10:00:00Z'))

        assert by_pipeline.call_args.kwargs['before_date'] == '2024-01-15'

    def test_rows_the_cursor_already_served_are_never_reconciled(self, mocker):
        """Reconciling costs an SFN describe per running run. A run newer than the
        cursor cannot appear on this page, so paying for it is pure waste."""
        from routes import executions
        mocker.patch.object(
            executions.executions_repo, 'query_runs_by_date',
            return_value=[_exec_row(pe='served', status='running',
                                    started='2024-01-15T12:00:00Z'),
                          _exec_row(pe='page', name='load-2024-01-15-x', status='running',
                                    started='2024-01-15T10:00:00Z')],
        )
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])
        mocker.patch.object(executions.pipelines_repo, 'get', return_value={'sfn_arn': 'arn:x'})
        reconcile = mocker.patch.object(executions, 'reconcile_sfn_status', return_value=None)

        executions.get_all_runs(_event(date='2024-01-15', before='2024-01-15T11:00:00Z'))

        assert [c.args[0] for c in reconcile.call_args_list] == ['page']


# ──────────────────────────────────────────────────────────────────────────────
# The two findings from the ADR #113 review — both silent data loss, both the same
# lie the cursor exists to remove, just one layer down.
# ──────────────────────────────────────────────────────────────────────────────

class TestNoSilentLoss:
    def test_a_split_run_never_renders_a_wrong_status(self, mocker):
        """The worst one. date-pipeline-index is ordered by pipeline_name, so a
        row-count cut lands mid-pipeline; /runs derives a run's status from the rows it
        got (ADR #112), so half a run's tasks reads as a *green* failed run. The read
        drops the pipeline it cut inside instead — a missing run beats a lying one."""
        from routes import executions

        def rows_for(pipeline, run, failing_task_included):
            tasks = ['t0', 't1'] + (['t2'] if failing_task_included else [])
            return [{'execution_name': f'{pipeline}-{t}-{run}', 'task_name': t,
                     'pipeline_name': pipeline, 'pipeline_execution': f'{pipeline}-r{run}',
                     'status': 'failed' if t == 't2' else 'success',
                     'date': '2024-01-15', 'started_at': f'2024-01-15T1{run}:00:00Z'}
                    for t in tasks]

        # `zulu` comes back without its failing task — i.e. it was cut mid-pipeline.
        mocker.patch.object(
            executions.executions_repo, 'query_runs_by_date',
            return_value=rows_for('alpha', 0, True) + rows_for('zulu', 1, False))
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])
        mocker.patch.object(executions.pipelines_repo, 'get', return_value=None)

        body = json.loads(executions.get_all_runs(_event(date='2024-01-15'))['body'])
        statuses = {r['pipeline_execution']: r['status'] for r in body['runs']}

        # This is what the repo read must prevent: it is the repo's job, pinned in
        # tests/dal/test_executions_repo_runs.py. Here we pin the consequence — the
        # route trusts the read, so the read may never hand it a partial run.
        assert statuses['alpha-r0'] == 'failed'
        assert statuses['zulu-r1'] == 'success'   # <- the lie, if the read ever splits

    def test_the_feed_reads_days_whole_pipelines_only(self, mocker):
        """The guard for the above: /runs must never use the row-capped read."""
        from routes import executions
        whole = mocker.patch.object(executions.executions_repo, 'query_runs_by_date',
                                    return_value=[])
        capped = mocker.patch.object(executions.executions_repo, 'query_by_date')
        mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])

        executions.get_all_runs(_event(date='2024-01-15'))
        executions.get_all_runs(_event())

        assert whole.call_count > 1
        capped.assert_not_called()

    def test_backfill_filters_run_before_the_page_is_cut(self, mocker):
        """?status=completed answered "no runs found" while completed backfills sat
        behind a run of recent `partial` ones: list_recent sliced to `limit` first, the
        filter emptied the page, and an empty page has no cursor — so the feed stopped
        there for good."""
        from routes import executions
        recent_noise = [_backfill_record(bf_id=f'bf-p{i}', status='partial',
                                         started=f'2024-01-15T{23 - i:02d}:00:00Z',
                                         keys=['2024-01-15']) for i in range(20)]
        wanted = [_backfill_record(bf_id='bf-done', status='completed',
                                   started='2024-01-14T01:00:00Z', keys=['2024-01-15'])]

        mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=[])
        list_recent = mocker.patch.object(executions.backfills_repo, 'list_recent',
                                          return_value=recent_noise + wanted)

        body = json.loads(
            executions.get_all_runs(_event(date='2024-01-15', status='completed', limit=5))['body'])

        assert [r['backfill_id'] for r in body['runs']] == ['bf-done']
        # and the repo must not have been asked to slice first
        assert list_recent.call_args.kwargs['limit'] is None
