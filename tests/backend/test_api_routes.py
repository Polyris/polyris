"""
API Route Tests for Console API

Tests critical business logic in routes/pipelines_list.py, routes/pipelines_info.py,
and routes/tasks.py with mocked DAL repositories.

Covers:
- list_pipelines: the registry's pipelines (the stats on run rows: console_api tests/routes/test_pipelines_list_runs.py)
- get_pipeline_status: the latest run of a date, stats counting, attempt and max_retries
- get_pipeline_dag: edges=[] handling, a 1.2 run's snapshot, the specs
"""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal
# pytest-mock: mocker fixture used instead of unittest.mock

os.environ.setdefault('DYNAMODB_TABLE', 'test-tokens')
os.environ.setdefault('PIPELINES_TABLE', 'test-registry')

TODAY = datetime.now(timezone.utc).strftime('%Y-%m-%d')


# ============================================================
# Mock helpers
# ============================================================

def _make_event(params=None, body=None):
    """Build minimal API Gateway event."""
    event = {}
    if params:
        event['queryStringParameters'] = params
    if body:
        event['body'] = json.dumps(body)
    return event


def _parse_response(response):
    """Parse API response body."""
    return json.loads(response['body'])


class MockRepo:
    """Mock DAL repository with all methods used by routes.

    Replaces the real repo singletons (executions_repo, pipelines_repo).
    Items are filtered in-memory to simulate DDB queries.
    """

    def __init__(self, items=None):
        self.items = items or []

    def get(self, key_value):
        """Match on any key field value."""
        for item in self.items:
            if key_value in item.values():
                return item
        return None

    def put(self, item):
        self.items.append(item)

    def update(self, key_value, update_expr, expr_values=None,
               expr_names=None, condition_expr=None):
        return {}

    def delete(self, key_value):
        pass

    def count(self):
        return len(self.items)

    def list_all(self, max_items=None, **kwargs):
        items = list(self.items)
        return items[:max_items] if max_items else items

    def scan(self, max_items=None, **kwargs):
        items = list(self.items)
        return items[:max_items] if max_items else items

    def scan_raw(self, **kwargs):
        items = list(self.items)
        limit = kwargs.get('Limit')
        if limit:
            items = items[:limit]
        return {'Items': items, 'Count': len(items)}

    def query_by_date(self, date, max_items=None, filter_expr=None,
                      projection=None, expr_names=None, key_condition=None,
                      select=None):
        items = [i for i in self.items if i.get('date') == date]
        return items[:max_items] if max_items else items

    def query_runs_by_date(self, date, min_rows, filter_expr=None,
                           projection=None, expr_names=None, key_condition=None):
        """Whole pipelines only (ADR #113). The fake holds one page's worth, so there
        is never a partial pipeline to drop — which is the real method's own answer
        when a date fits in one DynamoDB page. ``min_rows`` is a floor, so it never
        truncates here."""
        return [i for i in self.items if i.get('date') == date]

    def query_by_date_raw(self, **kwargs):
        return {'Items': self.items, 'Count': len(self.items)}

    def rows_of_pipeline_date(self, pipeline_name, date):
        """One pipeline's task rows of one date (pipeline-date-index), internal rows dropped."""
        return [i for i in self.items if i.get('pipeline_name') == pipeline_name and i.get('date') == date
                and not str(i.get('execution_name', '')).startswith(('_', 'dag_snapshot::'))]

    def query_by_pipeline_execution(self, pipeline_execution,
                                     projection=None, expr_names=None,
                                     limit=None):
        items = [i for i in self.items
                 if i.get('pipeline_execution') == pipeline_execution]
        return items[:limit] if limit else items


# ============================================================
# list_pipelines tests
# ============================================================

def test_list_pipelines_basic(mocker):
    """list_pipelines returns pipelines from registry."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily', 'description': 'Daily pipeline'},
        {'pipeline_name': 'weekly', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:weekly', 'description': ''},
    ])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event()))

    assert len(resp['pipelines']) == 2
    names = {p['name'] for p in resp['pipelines']}
    assert names == {'daily', 'weekly'}


def test_list_pipelines_schedule_from_registry(mocker):
    """list_pipelines returns schedule field from registry."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': '', 'schedule': 'cron(0 8 * * ? *)'},
        {'pipeline_name': 'weekly', 'sfn_arn': 'arn:...', 'description': '', 'schedule': 'cron(0 10 ? * MON *)'},
        {'pipeline_name': 'manual', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event()))

    by_name = {p['name']: p for p in resp['pipelines']}
    assert by_name['daily']['schedule'] == 'cron(0 8 * * ? *)'
    assert by_name['weekly']['schedule'] == 'cron(0 10 ? * MON *)'
    assert by_name['manual']['schedule'] == ''


# ============================================================
# get_pipeline_status tests
# ============================================================

def test_pipeline_status_stats_count_all_statuses(mocker):
    """Stats count succeeded, aborted, stopped correctly."""
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't1', 'status': 'success', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e2', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't2', 'status': 'succeeded', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e3', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't3', 'status': 'aborted', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e4', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't4', 'status': 'stopped', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e5', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't5', 'status': 'skipped', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
    ])
    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    stats = resp['stats']
    assert stats['success'] == 2
    assert stats['stopped'] == 2
    assert stats['skipped'] == 1
    assert stats['total'] == 5


def test_pipeline_status_attempt_and_max_retries_in_task(mocker):
    """attempt and max_retries are present on each task row returned by get_pipeline_status."""
    mock_execs = MockRepo([
        {'execution_name': 'extract-2026-01-01-abc', 'pipeline_name': 'daily',
         'pipeline_execution': 'exec-1', 'task_name': 'extract', 'status': 'failed',
         'date': TODAY, 'started_at': '2026-01-01T00:00:00Z',
         'attempt': Decimal('2'), 'restarts': Decimal('1'), 'task_config': '{"retries": 3, "timeout": 7200}'},
    ])
    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    task = resp['tasks'][0]
    assert task['attempt'] == 2, "attempt must be int, not Decimal"
    assert task['restarts'] == 1 and isinstance(task['restarts'], int)     # the operator's Restarts (↻)
    assert task['max_retries'] == 3


def test_pipeline_status_attempt_absent_returns_none(mocker):
    """A task row with no attempt or task_config in DDB yields attempt=None, max_retries=None."""
    mock_execs = MockRepo([
        {'execution_name': 'extract-2026-01-01-abc', 'pipeline_name': 'daily',
         'pipeline_execution': 'exec-1', 'task_name': 'extract', 'status': 'success',
         'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
    ])
    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    task = resp['tasks'][0]
    assert task['attempt'] is None
    assert task['restarts'] == 0
    assert task['max_retries'] is None


# ============================================================
# get_pipeline_dag tests
# ============================================================

def test_pipeline_dag_empty_edges(mocker, deploy_specs):
    """A pipeline whose tasks have no dependencies draws with edges=[] (its specs, ADR #137 §8)."""
    mock_pipelines = MockRepo([deploy_specs('parallel', [{'task_id': 'a'}, {'task_id': 'b'}])])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('parallel', _make_event()))

    assert [(n['id'], n['name']) for n in resp['nodes']] == [('a', 'a'), ('b', 'b')]
    assert resp['edges'] == []


def test_pipeline_dag_with_edges(mocker, deploy_specs):
    """A dependency is an edge from the upstream to its dependant."""
    mock_pipelines = MockRepo([deploy_specs('chain', [{'task_id': 'a'}, {'task_id': 'b', 'dependencies': ['a']}])])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('chain', _make_event()))

    assert resp['edges'] == [{'from': 'a', 'to': 'b'}]


def test_pipeline_dag_no_dag_in_registry(mocker):
    """Fallback to building DAG from task data when no dag in registry."""
    mock_pipelines = MockRepo([{'pipeline_name': 'nodag'}])
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'nodag', 'pipeline_execution': 'exec-1',
         'task_name': 't1', 'status': 'success', 'date': TODAY, 'dependencies': '[]'},
        {'execution_name': 'e2', 'pipeline_name': 'nodag', 'pipeline_execution': 'exec-1',
         'task_name': 't2', 'status': 'success', 'date': TODAY, 'dependencies': '["t1"]'},
    ])

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('nodag', _make_event({'date': TODAY})))

    assert len(resp['nodes']) == 2
    assert len(resp['edges']) == 1


def test_a_1_2_runs_dag_snapshot_is_not_read_the_pipelines_specs_are(mocker, deploy_specs):
    """ADR #137, *Migration and upgrade*: 1.4 reads no DAG snapshot of a 1.2 run; such a run shows the pipeline's current graph."""
    snapshot_dag = {'nodes': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}], 'edges': []}
    mock_pipelines = MockRepo([deploy_specs('daily', [{'task_id': 'a'}, {'task_id': 'b'}, {'task_id': 'd'}])])
    mock_execs = MockRepo([
        {'execution_name': 'dag_snapshot::exec-old', 'pipeline_name': 'daily',
         'pipeline_execution': 'exec-old', 'dag': json.dumps(snapshot_dag)},
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-old',
         'task_name': 'a', 'status': 'success', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event({
    'pipeline_execution': 'exec-old'
    })))

    node_ids = [n['id'] for n in resp['nodes']]
    assert 'c' not in node_ids
    assert 'd' in node_ids
    assert resp['dag_source'] == 'registry'


def test_pipeline_dag_falls_back_to_registry_without_snapshot(mocker, deploy_specs):
    """When no snapshot exists for execution, draw the specs of the version the registry names."""
    mock_pipelines = MockRepo([deploy_specs('daily', [{'task_id': 'a'}, {'task_id': 'b', 'dependencies': ['a']}])])
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 'a', 'status': 'success', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event({
    'pipeline_execution': 'exec-1'
    })))

    assert len(resp['nodes']) == 2
    assert resp['dag_source'] == 'registry'


def test_pipeline_dag_no_execution_uses_registry(mocker, deploy_specs):
    """Without pipeline_execution parameter, always draw the registry's version."""
    mock_pipelines = MockRepo([deploy_specs('daily', [{'task_id': 'x'}])])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('feed.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event()))

    assert resp['dag_source'] == 'registry'


# ============================================================
# _format_task_row tests — triggered_by, attempt, max_retries
# ============================================================

class TestFormatTaskRow:
    """_format_task_row projects triggered_by, attempt, and max_retries
    (parsed from the task_config JSON blob) into the feed row."""

    def _call(self, item):
        from routes.tasks import _format_task_row
        return _format_task_row(item)

    def test_triggered_by_passed_through(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY, 'triggered_by': 'console_run',
        })
        assert row['triggered_by'] == 'console_run'

    def test_triggered_by_absent_is_none(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY,
        })
        assert row['triggered_by'] is None

    def test_attempt_decimal_converted_to_int(self):
        """boto3.resource returns DDB N values as Decimal; safe_int must convert."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'running', 'date': TODAY, 'attempt': Decimal('2'), 'restarts': Decimal('1'),
        })
        assert row['attempt'] == 2
        assert isinstance(row['attempt'], int)
        assert row['restarts'] == 1 and isinstance(row['restarts'], int)

    def test_attempt_absent_is_none(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'waiting', 'date': TODAY,
        })
        assert row['attempt'] is None

    def test_max_retries_parsed_from_task_config(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'failed', 'date': TODAY,
            'task_config': '{"retries": 3, "timeout": 14400}',
        })
        assert row['max_retries'] == 3

    def test_max_retries_zero_when_not_in_task_config(self):
        """task_config present but no retries key → max_retries is None (retries=0 stores no key)."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY,
            'task_config': '{"timeout": 14400}',
        })
        assert row['max_retries'] is None

    def test_max_retries_none_when_task_config_absent(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY,
        })
        assert row['max_retries'] is None

    def test_max_retries_none_on_malformed_task_config(self):
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY,
            'task_config': 'not-valid-json{',
        })
        assert row['max_retries'] is None

    def test_max_retries_from_dict_task_config(self):
        """task_config stored as a DDB Map (dict after boto3 deserialize) is handled."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'success', 'date': TODAY,
            'task_config': {'retries': 3, 'timeout': 14400},
        })
        assert row['max_retries'] == 3

    # ──── ADR #125: retry_attempt surface ────

    def test_retry_attempt_absent_defaults_to_zero(self):
        """Pre-ADR #125 rows without retry_attempt render as 0, not None — the UI
        chip is gated on `> 0` so this ensures no accidental badge."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'running', 'date': TODAY,
        })
        assert row['retry_attempt'] == 0
        assert isinstance(row['retry_attempt'], int)

    def test_retry_attempt_bumped_during_in_sfn_retry(self):
        """Save_Retry_Attempt wrote retry_attempt=2 during the loop; _format_task_row
        must surface it so the UI's RefreshCw chip renders `2/N`."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'running', 'date': TODAY,
            'attempt': 1,            # no manual restart
            'retry_attempt': Decimal('2'),  # in-SFN auto-retry index
            'task_config': '{"retries": 3}',
        })
        assert row['attempt'] == 1
        assert row['retry_attempt'] == 2
        assert row['max_retries'] == 3

    def test_retry_attempt_distinct_from_attempt_after_restart_and_retry(self):
        """ADR #125 — the two axes stay separate: `attempt=2` means a user
        restarted once; `retry_attempt=1` means the current wrapper auto-retried
        once. The UI badge renders both chips distinctly."""
        row = self._call({
            'execution_name': 'e1', 'task_name': 't1', 'pipeline_name': 'p',
            'status': 'running', 'date': TODAY,
            'attempt': 2,
            'retry_attempt': 1,
        })
        assert row['attempt'] == 2
        assert row['retry_attempt'] == 1


# ============================================================
# get_task_config — max_retries field resolution
# ============================================================

class TestGetTaskConfig:
    """get_task_config must use resolve_max_retries so it reads the registration
    value (task_config.retries) of a row from before 1.3 — same contract as
    _format_task_row and get_pipeline_status."""

    def _call(self, mocker, item):
        execution_name = 'extract-2026-01-01-abc12345'
        mock_repo = MockRepo([{**item, 'execution_name': execution_name}])
        mocker.patch('routes.tasks.executions_repo', mock_repo)
        from routes.tasks import get_task_config
        resp = _parse_response(get_task_config(execution_name, _make_event({'date': TODAY})))
        return resp['config']

    def test_reads_max_retries_from_task_config_blob(self, mocker):
        """Registration-only task: max_retries comes from task_config.retries, not a default."""
        cfg = self._call(mocker, {'task_name': 'extract', 'task_config': '{"retries": 5}'})
        assert cfg['max_retries'] == 5

    def test_returns_none_when_no_retry_config(self, mocker):
        """No fabricated default: when neither field is set, max_retries is null."""
        cfg = self._call(mocker, {'task_name': 'extract'})
        assert cfg['max_retries'] is None


# ============================================================
# ADR #125 — get_task_events history endpoint
# ============================================================

class TestGetTaskEventsRetryAttempt:
    """get_task_events must surface the new retry_attempt field so the UI
    history line can distinguish initial dispatch, in-SFN retries, and the
    terminating TASK_FINISHED. The sort-key suffix (#15-N) must be stripped
    from event_time before returning to the client, matching the existing
    convention for TASK_STARTED's #10 suffix."""

    def _invoke(self, mocker, events):
        mock_repo = MockRepo([{'execution_name': 'e1', 'task_run_id': 'trid1'}])
        mocker.patch('routes.tasks.executions_repo', mock_repo)
        mocker.patch(
            'routes.tasks.task_events_repo.query_by_task_run_id',
            return_value=events,
        )
        from routes.tasks import get_task_events
        return _parse_response(get_task_events('e1', _make_event({})))

    def test_retry_started_event_surfaces_retry_attempt(self, mocker):
        """A TASK_RETRY_STARTED event with retry_attempt=1 is returned verbatim,
        with the #15-N sort suffix stripped from event_time."""
        resp = self._invoke(mocker, [
            {
                'event_type': 'TASK_STARTED',
                'event_time': '2026-10-08T13:26:53.100Z#10',
                'attempt': 1, 'retry_attempt': 0,
                'task_name': 't1', 'task_type': 'glue',
            },
            {
                'event_type': 'TASK_RETRY_STARTED',
                'event_time': '2026-10-08T13:31:53.100Z#15-1',
                'attempt': 1, 'retry_attempt': 1,
                'task_name': 't1', 'task_type': 'glue',
            },
        ])
        evts = resp['events']
        assert evts[0]['event_type'] == 'TASK_STARTED'
        assert evts[0]['retry_attempt'] == 0
        assert evts[1]['event_type'] == 'TASK_RETRY_STARTED'
        assert evts[1]['retry_attempt'] == 1
        # Sort-key suffix stripped — the UI shows ISO time, not the DDB sort key.
        assert '#' not in evts[1]['event_time']

    def test_pre_adr_events_default_retry_attempt_to_zero(self, mocker):
        """Legacy events (pre-ADR #125) without retry_attempt in the Item default
        to 0 via safe_int — the UI chip stays hidden rather than crashing."""
        resp = self._invoke(mocker, [
            {
                'event_type': 'TASK_STARTED',
                'event_time': '2026-10-08T13:26:53.100Z#10',
                'attempt': 1,
                'task_name': 't1',
            },
        ])
        assert resp['events'][0]['retry_attempt'] == 0

    def test_task_finished_carries_retry_attempt_for_timeline_grouping(self, mocker):
        """A failed retry's TASK_FINISHED must carry retry_attempt so the timeline
        can show 'failed after retry 2' instead of just 'failed'."""
        resp = self._invoke(mocker, [
            {
                'event_type': 'TASK_FINISHED',
                'event_time': '2026-10-08T13:40:00.000Z#20',
                'attempt': 1, 'retry_attempt': 2,
                'status': 'failed',
                'error_summary': 'boom',
            },
        ])
        evt = resp['events'][0]
        assert evt['status'] == 'failed'
        assert evt['retry_attempt'] == 2
