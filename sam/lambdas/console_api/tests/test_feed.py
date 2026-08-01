"""
Unit tests for `feed` — the shared paging behind the History feeds (/runs + /tasks).

The contract that matters (and that the old `[:limit]` slice broke):
  - a page never re-serves what an earlier page already showed, and never skips
    anything between them;
  - `next` is None **only** when nothing older exists — that is what lets the UI stop
    claiming "50 runs" when it means "the first 50 of who knows how many";
  - the cross-pipeline walk starts at the cursor's date and stays inside the window;
  - the pipeline-index read keeps pulling past a cut date it has already served,
    instead of stalling on it forever.

Pattern follows test_executions_repo_runs.py — pytest-mock (ADR #26), boundary mocks
only (the repo / the clock); the paging logic itself runs for real.
"""

from datetime import datetime, timedelta, timezone

import pytest

from constants import Limits
import feed
from feed import feed_dates, is_older, page_by_started_at, pipeline_rows_before


TODAY = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def frozen_today(mocker):
    """Freeze the clock feed_dates walks back from (CLAUDE.md #14 — time is a boundary)."""
    mocker.patch.object(feed, 'datetime', mocker.Mock(now=lambda tz=None: TODAY))
    return TODAY.strftime('%Y-%m-%d')


def _row(started_at, **extra):
    return {'started_at': started_at, **extra}


# ──────────────────────────────────────────────────────────────────────────────
# is_older — the cursor predicate
# ──────────────────────────────────────────────────────────────────────────────

class TestIsOlder:
    def test_no_cursor_keeps_everything(self):
        assert is_older(_row('2026-07-16T10:00:00Z'), '') is True

    def test_strictly_older_is_kept(self):
        assert is_older(_row('2026-07-16T09:00:00Z'), '2026-07-16T10:00:00Z') is True

    def test_the_cursor_row_itself_is_dropped(self):
        """Strict `<`, so the last row of the previous page is not served twice."""
        assert is_older(_row('2026-07-16T10:00:00Z'), '2026-07-16T10:00:00Z') is False

    def test_newer_is_dropped(self):
        assert is_older(_row('2026-07-16T11:00:00Z'), '2026-07-16T10:00:00Z') is False

    def test_missing_started_at_counts_as_oldest(self):
        """It is where such a row sorts, so it must be where the cursor puts it too."""
        assert is_older({}, '2026-07-16T10:00:00Z') is True


# ──────────────────────────────────────────────────────────────────────────────
# feed_dates — the cross-pipeline walk (ADR #108: `date` is the shard key)
# ──────────────────────────────────────────────────────────────────────────────

class TestFeedDates:
    def test_no_cursor_walks_the_whole_window_from_today(self, frozen_today):
        dates = feed_dates('')
        assert dates[0] == '2026-07-16'
        assert len(dates) == Limits.SLA_DAYS
        assert dates[-1] == '2026-07-03'          # SLA_DAYS inclusive of today

    def test_dates_are_contiguous_and_newest_first(self, frozen_today):
        dates = feed_dates('')
        assert dates == sorted(dates, reverse=True)
        span = [(datetime.strptime(d, '%Y-%m-%d')) for d in dates]
        assert all(a - b == timedelta(days=1) for a, b in zip(span, span[1:]))

    def test_cursor_starts_the_page_at_its_own_date(self, frozen_today):
        """Everything newer was already served — no reason to re-read those days."""
        dates = feed_dates('2026-07-14T08:30:00Z')
        assert dates[0] == '2026-07-14'
        assert '2026-07-15' not in dates

    def test_paging_deeper_shortens_the_walk(self, frozen_today):
        assert len(feed_dates('2026-07-14T08:30:00Z')) == Limits.SLA_DAYS - 2

    def test_walk_never_leaves_the_window(self, frozen_today):
        """A cursor older than the window yields nothing rather than querying past it."""
        assert feed_dates('2026-06-01T00:00:00Z') == []

    def test_future_cursor_is_clamped_to_today(self, frozen_today):
        """Never query days that cannot exist yet, whatever the client sends."""
        assert feed_dates('2027-01-01T00:00:00Z')[0] == '2026-07-16'
        assert len(feed_dates('2027-01-01T00:00:00Z')) == Limits.SLA_DAYS

    def test_unparseable_cursor_degrades_to_today(self, frozen_today, mocker):
        """A bad query param costs the cursor, not the feed (same as safe_param_int)."""
        warn = mocker.patch.object(feed.log, 'warn')
        assert feed_dates('not-a-timestamp')[0] == '2026-07-16'
        warn.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# page_by_started_at — the honest cut
# ──────────────────────────────────────────────────────────────────────────────

class TestPageByStartedAt:
    def test_sorts_newest_first(self):
        page, _ = page_by_started_at(
            [_row('2026-07-14T10:00:00Z'), _row('2026-07-16T10:00:00Z'), _row('2026-07-15T10:00:00Z')],
            '', 10,
        )
        assert [r['started_at'] for r in page] == [
            '2026-07-16T10:00:00Z', '2026-07-15T10:00:00Z', '2026-07-14T10:00:00Z']

    def test_no_cursor_when_everything_fits(self):
        """The whole point: "that is all" must be sayable, and true."""
        page, cursor = page_by_started_at([_row('2026-07-16T10:00:00Z')], '', 10)
        assert len(page) == 1
        assert cursor is None

    def test_empty_feed_has_no_cursor(self):
        assert page_by_started_at([], '', 10) == ([], None)

    def test_truncated_page_hands_back_its_last_row_as_the_cursor(self):
        rows = [_row(f'2026-07-16T10:0{i}:00Z') for i in range(5)]
        page, cursor = page_by_started_at(rows, '', 3)
        assert len(page) == 3
        assert cursor == page[-1]['started_at'] == '2026-07-16T10:02:00Z'

    def test_pages_partition_the_feed_exactly(self):
        """No row served twice, none skipped — walk the whole feed page by page."""
        rows = [_row(f'2026-07-16T10:{i:02d}:00Z') for i in range(10)]
        seen, cursor = [], ''
        for _ in range(10):                       # bounded: guards against a stall
            page, cursor = page_by_started_at(rows, cursor, 3)
            seen.extend(r['started_at'] for r in page)
            if cursor is None:
                break
        assert cursor is None
        assert seen == sorted((r['started_at'] for r in rows), reverse=True)
        assert len(seen) == len(set(seen)) == 10

    def test_exactly_limit_rows_is_not_a_next_page(self):
        """`len(rows) == limit` means the feed ended on a page boundary, not that
        there is more — an off-by-one here shows the user an empty "show older"."""
        rows = [_row(f'2026-07-16T10:0{i}:00Z') for i in range(3)]
        page, cursor = page_by_started_at(rows, '', 3)
        assert len(page) == 3
        assert cursor is None

    def test_a_page_never_ends_inside_a_group_of_identical_timestamps(self):
        """Regression. The cursor is a strict `<`, so a row sharing the boundary's
        timestamp would be filtered out by the very cursor meant to fetch it — lost for
        good. The page absorbs its twins instead; that is what makes a plain timestamp
        cursor sufficient, rather than a compromise needing a composite key."""
        rows = ([_row('2026-07-16T12:00:00.000Z', id=f'a{i}') for i in range(3)]   # twins
                + [_row('2026-07-16T11:00:00.000Z', id='old')])

        page, cursor = page_by_started_at(rows, '', 1)

        assert len(page) == 3                     # ran long rather than split the instant
        assert {r['id'] for r in page} == {'a0', 'a1', 'a2'}
        assert cursor == '2026-07-16T12:00:00.000Z'

        page2, cursor2 = page_by_started_at(rows, cursor, 1)
        assert [r['id'] for r in page2] == ['old']
        assert cursor2 is None

    def test_identical_timestamps_at_the_tail_are_not_a_next_page(self):
        """Absorbing the twins can consume the rest of the feed — say so."""
        rows = [_row('2026-07-16T12:00:00.000Z', id=f'a{i}') for i in range(3)]
        page, cursor = page_by_started_at(rows, '', 2)
        assert len(page) == 3
        assert cursor is None

    def test_walking_a_feed_of_ties_loses_nothing(self):
        rows = ([_row('2026-07-16T12:00:00.000Z', id=f'a{i}') for i in range(3)]
                + [_row('2026-07-16T11:00:00.000Z', id=f'b{i}') for i in range(3)]
                + [_row('2026-07-16T10:00:00.000Z', id='c0')])
        seen, cursor = [], ''
        for _ in range(10):
            page, cursor = page_by_started_at(rows, cursor, 2)
            seen.extend(r['id'] for r in page)
            if cursor is None:
                break
        assert sorted(seen) == ['a0', 'a1', 'a2', 'b0', 'b1', 'b2', 'c0']
        assert len(seen) == len(set(seen))

    def test_rows_without_started_at_sort_last_and_end_paging(self):
        """Broken-data guard: such a row cannot be a cursor, so paging stops on it
        rather than looping on a cursor that would re-serve it forever."""
        rows = [_row('2026-07-16T10:00:00Z'), {'execution_name': 'no-timestamp'}]
        page, cursor = page_by_started_at(rows, '', 1)
        assert page[0]['started_at'] == '2026-07-16T10:00:00Z'
        assert cursor == '2026-07-16T10:00:00Z'

        page, cursor = page_by_started_at(rows, cursor, 1)
        assert page == [{'execution_name': 'no-timestamp'}]
        assert cursor is None


# ──────────────────────────────────────────────────────────────────────────────
# pipeline_rows_before — the windowless index read (ADR #108)
# ──────────────────────────────────────────────────────────────────────────────

def _idx_row(date, execution, started_at):
    return {'date': date, 'pipeline_execution': execution, 'started_at': started_at}


def _count_all(rows):
    return len(rows)


class TestPipelineRowsBefore:
    def test_one_read_is_enough_when_the_first_chunk_overshoots_the_page(self, mocker):
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline',
            return_value=([_idx_row('2026-07-16', 'e1', '2026-07-16T10:00:00Z'),
                           _idx_row('2026-07-16', 'e2', '2026-07-16T09:00:00Z')], '2026-07-15'),
        )
        rows = pipeline_rows_before('p', '', 1, count_fn=_count_all)
        assert len(rows) == 2
        assert q.call_count == 1

    def test_asks_the_repo_for_one_more_than_the_page(self, mocker):
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline', return_value=([], None))
        pipeline_rows_before('p', '', 50, count_fn=_count_all)
        assert q.call_args.kwargs['min_runs'] == 51

    def test_reads_past_the_page_boundary_rather_than_stopping_on_it(self, mocker):
        """Regression. page_by_started_at infers "there is more" from having more rows
        than fit, so stopping at exactly `want` makes a full page indistinguishable from
        the end of the feed — and strands every row behind it. The boundary must be
        overshot by at least one, or reached only by exhausting the index."""
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline',
            side_effect=[
                ([_idx_row('2026-07-16', f'e{i}', f'2026-07-16T1{i}:00:00Z') for i in range(5)],
                 '2026-07-15'),                                    # exactly `want`, more behind
                ([_idx_row('2026-07-15', 'e9', '2026-07-15T10:00:00Z')], None),
            ],
        )
        rows = pipeline_rows_before('p', '', 5, count_fn=_count_all)
        assert len(rows) > 5                 # the extra row is what the pager reads
        assert q.call_count == 2

    def test_exhaustion_is_the_only_way_to_land_on_exactly_the_page_size(self, mocker):
        """The mirror of the above: `want` rows and nothing behind them really is the
        end, and must not cost a second read or invent a next page."""
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline',
            return_value=([_idx_row('2026-07-16', f'e{i}', f'2026-07-16T1{i}:00:00Z')
                           for i in range(5)], None),
        )
        rows = pipeline_rows_before('p', '', 5, count_fn=_count_all)
        assert len(rows) == 5
        assert q.call_count == 1

    def test_cursor_date_seeds_the_index_read(self, mocker):
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline', return_value=([], None))
        pipeline_rows_before('p', '2026-07-14T08:30:00Z', 5, count_fn=_count_all)
        assert q.call_args.kwargs['before_date'] == '2026-07-14'

    def test_no_cursor_reads_from_the_newest_date(self, mocker):
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline', return_value=([], None))
        pipeline_rows_before('p', '', 5, count_fn=_count_all)
        assert q.call_args.kwargs['before_date'] is None

    def test_keeps_reading_past_a_cut_date_it_already_served(self, mocker):
        """The stall this loop exists for: the repo's cursor is a *date*, so a busy
        cut date hands back only rows the cursor already served. Without another read
        the page would be empty and the older dates behind it unreachable."""
        before = '2026-07-16T10:00:00Z'
        served = [_idx_row('2026-07-16', 'e1', '2026-07-16T11:00:00Z')]   # newer than cursor
        older = [_idx_row('2026-07-15', 'e2', '2026-07-15T09:00:00Z')]
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline',
            side_effect=[(served, '2026-07-15'), (older, None)],
        )

        rows = pipeline_rows_before(
            'p', before, 1, count_fn=lambda rs: sum(1 for r in rs if is_older(r, before)))

        assert q.call_count == 2
        assert q.call_args_list[1].kwargs['before_date'] == '2026-07-15'
        assert older[0] in rows

    def test_stops_when_the_index_runs_out_even_if_the_page_is_short(self, mocker):
        """Exhaustion beats the page size — an honest short last page, not a loop."""
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline',
            return_value=([_idx_row('2026-07-16', 'e1', '2026-07-16T10:00:00Z')], None),
        )
        rows = pipeline_rows_before('p', '', 50, count_fn=_count_all)
        assert len(rows) == 1
        assert q.call_count == 1

    def test_forwards_projection_and_expression_names(self, mocker):
        q = mocker.patch.object(
            feed.executions_repo, 'query_runs_by_pipeline', return_value=([], None))
        pipeline_rows_before('p', '', 5, count_fn=_count_all,
                             projection='started_at, #s', expr_names={'#s': 'status'})
        assert q.call_args.kwargs['projection'] == 'started_at, #s'
        assert q.call_args.kwargs['expr_names'] == {'#s': 'status'}
