"""
dal.backfills_repo.BackfillsRepo against the tokens table of sam/template.yaml, in moto.

Covers single-item ops (get/put/put_if_new/update_status/increment_counter),
the search (list_recent/list_active/list_active_for_pipeline/list_retries_of)
on ``pipeline-date-index`` with the records read in full by BatchGetItem
(ADR #137 §5), child executions on ``backfill-id-index``, and the lifetimes:
the history's while a backfill runs, 90 days once its status is final.
"""
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from polyris.coordination import retention

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

SENTINEL = '_polyris_bulk_backfill'


@pytest.fixture
def tokens(mocker):
    import config

    logical, name = kit.TABLES["TOKENS_TABLE"]
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        kit.create_tables(resource, {logical: name})
        mocker.patch.object(config, "_dynamodb", resource)
        yield resource.Table(name)


@pytest.fixture
def repo(tokens):
    from dal.backfills_repo import BackfillsRepo

    repo = BackfillsRepo()
    repo._table_name = tokens.name
    return repo


def _record(repo, backfill_id, started_at=None, **fields):
    item = {'execution_name': backfill_id, 'backfill_id': backfill_id, 'status': 'running', **fields}
    if started_at:
        item['started_at'] = started_at
    repo.put(item)


def _ids(items):
    return [i['execution_name'] for i in items]


def _about(ttl, lifetime):
    expected = retention.ttl(datetime.now(timezone.utc), lifetime)
    return abs(int(ttl) - expected) <= 60


# ──────────────────────────────────────────────────────────────────────────────
# Single-item ops
# ──────────────────────────────────────────────────────────────────────────────

class TestGet:
    def test_returns_backfill_record(self, repo):
        _record(repo, 'bf-abc123')
        result = repo.get('bf-abc123')
        assert result is not None
        assert result['execution_name'] == 'bf-abc123'

    def test_returns_none_if_missing(self, repo):
        assert repo.get('bf-nonexistent') is None

    def test_returns_none_if_not_backfill_record(self, repo, tokens):
        """Defensive: a row at the key that is no Backfill is not returned."""
        tokens.put_item(Item={'execution_name': 'bf-collides', 'record_type': 'execution'})
        assert repo.get('bf-collides') is None

    def test_a_consistent_read_returns_the_record(self, repo):
        _record(repo, 'bf-abc')
        assert repo.get('bf-abc', consistent=True)['record_type'] == 'backfill'


class TestPut:
    def test_put_sets_the_sentinel_and_the_kind(self, repo, tokens):
        repo.put({'execution_name': 'bf-abc', 'status': 'pending'})
        item = tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']
        assert item['pipeline_name'] == SENTINEL
        assert item['record_type'] == 'backfill'

    def test_put_dates_the_record_by_the_day_it_started(self, repo, tokens):
        repo.put({'execution_name': 'bf-abc', 'started_at': '2026-11-03T23:59:00+00:00'})
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['date'] == '2026-11-03'

    def test_a_record_without_a_start_is_dated_today(self, repo, tokens):
        repo.put({'execution_name': 'bf-abc'})
        today = datetime.now(timezone.utc).date().isoformat()
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['date'] == today

    def test_a_running_backfill_lives_as_long_as_a_run_s_history(self, repo, tokens):
        repo.put({'execution_name': 'bf-abc'})
        assert _about(tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['ttl'], retention.BACKFILL_RUNNING)

    def test_put_preserves_caller_provided_fields(self, repo, tokens):
        repo.put({'execution_name': 'bf-abc', 'ttl': 999999, 'date': '2026-01-01', 'status': 'running'})
        item = tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']
        assert (item['ttl'], item['date']) == (999999, '2026-01-01')


class TestPutIfNew:
    """Conditional put — handles bf-id collisions safely."""

    def test_returns_true_on_success(self, repo, tokens):
        assert repo.put_if_new({'execution_name': 'bf-abc'}) is True
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['record_type'] == 'backfill'

    def test_returns_false_on_collision_and_keeps_the_first(self, repo, tokens):
        assert repo.put_if_new({'execution_name': 'bf-abc', 'status': 'completed'}) is True
        assert repo.put_if_new({'execution_name': 'bf-abc', 'status': 'pending'}) is False
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['status'] == 'completed'

    def test_raises_on_other_client_error(self, repo):
        """A missing table, throttling or IAM must reach the caller, which answers 5xx."""
        repo._table_name = 'no-such-table'
        with pytest.raises(ClientError):
            repo.put_if_new({'execution_name': 'bf-abc'})

    def test_applies_same_defaults_as_put(self, repo, tokens):
        repo.put_if_new({'execution_name': 'bf-abc', 'started_at': '2026-11-03T10:00:00+00:00'})
        item = tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']
        assert (item['pipeline_name'], item['record_type'], item['date']) == (SENTINEL, 'backfill', '2026-11-03')
        assert _about(item['ttl'], retention.BACKFILL_RUNNING)


class TestUpdateStatus:
    def test_a_status_that_goes_on_keeps_the_running_lifetime(self, repo):
        _record(repo, 'bf-abc', status='pending')
        result = repo.update_status('bf-abc', 'running')
        assert result['status'] == 'running'
        assert _about(result['ttl'], retention.BACKFILL_RUNNING)

    @pytest.mark.parametrize('final', ['completed', 'failed', 'partial', 'canceled'])
    def test_a_final_status_leaves_the_record_90_days(self, repo, final):
        _record(repo, 'bf-abc')
        result = repo.update_status('bf-abc', final)
        assert result['status'] == final
        assert _about(result['ttl'], retention.BACKFILL_ENDED)

    def test_the_condition_refuses_a_status_it_does_not_list(self, repo, tokens):
        """The cancel flow (ADR #54) cancels only a pending or running Backfill."""
        _record(repo, 'bf-abc', status='completed')
        with pytest.raises(ClientError) as raised:
            repo.update_status('bf-abc', 'canceled', condition_status_in=['pending', 'running'])
        assert raised.value.response['Error']['Code'] == 'ConditionalCheckFailedException'
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item']['status'] == 'completed'

    def test_the_condition_accepts_a_status_it_lists(self, repo):
        _record(repo, 'bf-abc', status='pending')
        assert repo.update_status('bf-abc', 'canceled', condition_status_in=['pending', 'running'])['status'] == \
            'canceled'


class TestIncrementCounter:
    @pytest.mark.parametrize('counter', ['completed_partitions', 'failed_partitions', 'skipped_partitions'])
    def test_counters_add_up(self, repo, tokens, counter):
        _record(repo, 'bf-abc')
        repo.increment_counter('bf-abc', counter)
        repo.increment_counter('bf-abc', counter, by=2)
        assert tokens.get_item(Key={'execution_name': 'bf-abc'})['Item'][counter] == 3

    def test_invalid_counter_name_rejected(self, repo):
        with pytest.raises(ValueError, match="Invalid counter name"):
            repo.increment_counter('bf-abc', 'arbitrary_field')

    def test_invalid_injection_attempt_rejected(self, repo):
        """Defense against UpdateExpression injection via counter_name."""
        with pytest.raises(ValueError):
            repo.increment_counter('bf-abc', 'completed_partitions, status = :evil')


# ──────────────────────────────────────────────────────────────────────────────
# The search: pipeline-date-index, then BatchGetItem
# ──────────────────────────────────────────────────────────────────────────────

class TestListRecent:
    def test_returns_only_backfill_records_newest_first(self, repo, tokens):
        _record(repo, 'bf-x', '2026-01-15T10:00:00+00:00')
        _record(repo, 'bf-y', '2026-01-16T10:00:00+00:00')
        tokens.put_item(Item={'execution_name': 'load-2026-01-16-abc', 'pipeline_name': 'orders',
                              'date': '2026-01-16', 'status': 'failed'})
        assert _ids(repo.list_recent(limit=10)) == ['bf-y', 'bf-x']

    def test_reads_every_field_of_a_record(self, repo):
        """The index projects few fields; the search reads the records themselves."""
        _record(repo, 'bf-x', '2026-01-15T10:00:00+00:00', target_pipeline='orders', total_partitions=7)
        [item] = repo.list_recent(limit=10)
        assert (item['target_pipeline'], item['total_partitions']) == ('orders', 7)

    def test_respects_limit(self, repo):
        for i in range(1, 6):
            _record(repo, f'bf-{i}', f'2026-01-{i:02d}T00:00:00+00:00')
        assert _ids(repo.list_recent(limit=3)) == ['bf-5', 'bf-4', 'bf-3']

    def test_a_limit_keeps_every_record_of_the_newest_days(self, repo):
        """It stops on a day boundary: records of one day are never half read."""
        _record(repo, 'bf-a', '2026-01-02T08:00:00+00:00')
        _record(repo, 'bf-b', '2026-01-02T09:00:00+00:00')
        _record(repo, 'bf-c', '2026-01-01T09:00:00+00:00')
        assert _ids(repo.list_recent(limit=1)) == ['bf-b']
        assert _ids(repo.list_recent(limit=2)) == ['bf-b', 'bf-a']

    def test_missing_started_at_sorts_last(self, repo):
        _record(repo, 'bf-a', '2026-01-15T10:00:00+00:00')
        _record(repo, 'bf-b', date='2026-01-15')
        assert _ids(repo.list_recent(limit=10)) == ['bf-a', 'bf-b']

    def test_a_record_written_before_1_3_has_no_date_and_is_not_found(self, repo, tokens):
        tokens.put_item(Item={'execution_name': 'bf-old', 'pipeline_name': SENTINEL, 'record_type': 'backfill',
                              'status': 'completed'})
        assert repo.list_recent(limit=None) == []

    def test_more_records_than_one_batch_get_takes(self, repo):
        from dal.backfills_repo import BATCH_GET

        count = BATCH_GET + 20
        for i in range(count):
            _record(repo, f'bf-{i:04d}', f'2026-01-{1 + i % 28:02d}T{i % 24:02d}:00:00+00:00')
        assert len(repo.list_recent(limit=None)) == count


class TestListRecentCursor:
    """`before` — the History feed's started_at cursor (console_api/feed.py, ADR #95)."""

    @pytest.fixture
    def five(self, repo):
        for i in range(1, 6):
            _record(repo, f'bf-{i}', f'2026-01-{i:02d}T00:00:00+00:00')
        return repo

    def test_returns_only_backfills_older_than_the_cursor(self, five):
        assert _ids(five.list_recent(limit=10, before='2026-01-03T00:00:00+00:00')) == ['bf-2', 'bf-1']

    def test_the_cursor_row_itself_is_not_re_served(self, five):
        """Strict `<`: the last row of the previous page must not come back."""
        assert 'bf-3' not in _ids(five.list_recent(limit=10, before='2026-01-03T00:00:00+00:00'))

    def test_cursor_is_applied_before_the_limit(self, five):
        """With the cursor applied *after* the slice, every page but the first comes back empty."""
        assert _ids(five.list_recent(limit=2, before='2026-01-03T00:00:00+00:00')) == ['bf-2', 'bf-1']

    def test_records_of_the_cursor_s_day_that_it_drops_do_not_fill_the_page(self, repo):
        """The cursor's own day may hold records newer than the cursor: they count toward no limit."""
        _record(repo, 'bf-late', '2026-01-03T12:00:00+00:00')
        _record(repo, 'bf-later', '2026-01-03T13:00:00+00:00')
        _record(repo, 'bf-old', '2026-01-02T00:00:00+00:00')
        assert _ids(repo.list_recent(limit=2, before='2026-01-03T11:00:00+00:00')) == ['bf-old']

    def test_no_cursor_is_unchanged_behaviour(self, five):
        assert len(five.list_recent(limit=10)) == 5

    def test_missing_started_at_counts_as_oldest(self, repo):
        """It sorts last, so the cursor keeps it rather than drops it."""
        _record(repo, 'bf-a', '2026-01-15T10:00:00+00:00')
        _record(repo, 'bf-b', date='2026-01-15')
        assert _ids(repo.list_recent(limit=10, before='2026-01-15T10:00:00+00:00')) == ['bf-b']


class TestListActive:
    def test_filters_to_pending_and_running(self, repo):
        _record(repo, 'bf-x', '2026-01-15T10:00:00+00:00', status='running')
        _record(repo, 'bf-y', '2026-01-15T11:00:00+00:00', status='pending')
        _record(repo, 'bf-z', '2026-01-15T12:00:00+00:00', status='completed')
        assert sorted(_ids(repo.list_active())) == ['bf-x', 'bf-y']

    def test_for_one_pipeline(self, repo):
        _record(repo, 'bf-x', '2026-01-15T10:00:00+00:00', target_pipeline='orders')
        _record(repo, 'bf-y', '2026-01-15T11:00:00+00:00', target_pipeline='billing')
        _record(repo, 'bf-z', '2026-01-15T12:00:00+00:00', target_pipeline='orders', status='failed')
        assert _ids(repo.list_active_for_pipeline('orders')) == ['bf-x']


class TestQueryChildExecutions:
    def test_uses_backfill_id_index(self, repo, tokens):
        for name in ('task-2026-01-15-abc', 'task-2026-01-16-abc'):
            tokens.put_item(Item={'execution_name': name, 'backfill_id': 'bf-x'})
        tokens.put_item(Item={'execution_name': 'task-2026-01-16-def', 'backfill_id': 'bf-other'})
        assert sorted(_ids(repo.query_child_executions('bf-x'))) == ['task-2026-01-15-abc', 'task-2026-01-16-abc']

    def test_returns_empty_for_unknown_backfill(self, repo):
        assert repo.query_child_executions('bf-nonexistent') == []


class TestListRetriesOf:
    """v0.78.11, ADR #68 — retry chain helper."""

    def test_filters_by_parent_backfill_id_in_start_order(self, repo):
        _record(repo, 'bf-retry2', '2026-01-15T11:00:00+00:00', parent_backfill_id='bf-parent')
        _record(repo, 'bf-retry1', '2026-01-15T10:00:00+00:00', parent_backfill_id='bf-parent')
        _record(repo, 'bf-other', '2026-01-15T12:00:00+00:00', parent_backfill_id='bf-else')
        assert _ids(repo.list_retries_of('bf-parent')) == ['bf-retry1', 'bf-retry2']

    def test_returns_empty_when_no_retries(self, repo):
        _record(repo, 'bf-x', '2026-01-15T10:00:00+00:00')
        assert repo.list_retries_of('bf-orphan') == []


def test_records_of_many_days_are_all_found(repo):
    """Every day is read, the newest first, however far back."""
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for i in range(0, 400, 40):
        _record(repo, f'bf-{i:03d}', (start + timedelta(days=i)).isoformat())
    assert _ids(repo.list_recent(limit=None)) == [f'bf-{i:03d}' for i in range(360, -1, -40)]
