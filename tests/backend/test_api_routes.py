"""
API Route Tests for Console API

Tests critical business logic in routes/pipelines_list.py, routes/pipelines_info.py,
and routes/tasks.py with mocked DAL repositories.

Covers:
- list_pipelines: stats calculation, status derivation, progress, SLA
- get_pipeline_status: reconciliation, stats counting, sfn_arn field
- get_pipeline_dag: edges=[] handling, snapshot versioning
- _reconcile_orphaned_tasks: orphan detection and abort marking
"""

import json
import os
from datetime import datetime, timezone, timedelta
# pytest-mock: mocker fixture used instead of unittest.mock
from botocore.exceptions import ClientError

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

    def query_by_pipeline_execution(self, pipeline_execution,
                                     projection=None, expr_names=None,
                                     limit=None):
        items = [i for i in self.items
                 if i.get('pipeline_execution') == pipeline_execution]
        return items[:limit] if limit else items

    def get_sfn_arn(self, pipeline_name):
        item = self.get(pipeline_name)
        if not item:
            return None
        return item.get('sfn_arn', '') or item.get('arn')

    @property
    def conditional_check_exception(self):
        return ClientError


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
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event()))

    assert len(resp['pipelines']) == 2
    names = {p['name'] for p in resp['pipelines']}
    assert names == {'daily', 'weekly'}


def test_list_pipelines_stats_succeeded(mocker):
    """Stats show succeeded when all tasks are success/skipped."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'skipped', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['status'] == 'success'
    assert p['progress'] == 100
    assert p['today_stats']['success'] == 2
    assert p['today_stats']['skipped'] == 1


def test_list_pipelines_stats_failed(mocker):
    """Card status is 'failed' when the (last) run failed (ADR #112 / option c)."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'failed', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'waiting', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['status'] == 'failed'
    assert p['today_stats']['failed'] == 1
    assert p['today_stats']['waiting'] == 1


def test_list_pipelines_status_is_last_run(mocker):
    """Card status reflects the MOST RECENT run, not a per-day aggregate (ADR #112 / c).

    An older run failed, but the latest run (a later date) succeeded — the card is
    'success'. today_stats/progress stay day-scoped and are unaffected.
    """
    yesterday = (datetime.fromisoformat(TODAY) - timedelta(days=1)).strftime('%Y-%m-%d')
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        # Older run (yesterday) — failed
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-old', 'status': 'failed', 'date': yesterday},
        # Latest run (today) — success
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-new', 'status': 'success', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['status'] == 'success', "card should reflect the latest run, not the older failed one"
    # Both runs appear in the sparkline, newest first.
    assert p['recent_runs'][0]['status'] == 'success'


def test_list_pipelines_stats_running(mocker):
    """Stats show running when tasks are in progress."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'running', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'waiting', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['status'] == 'running'
    assert p['progress'] == 33


def test_list_pipelines_sla_excludes_aborted(mocker):
    """SLA excludes aborted (user-stopped) runs from the denominator (ADR #112).

    Runs: success, aborted, failed. Aborted is not a system failure, so it is
    excluded entirely — SLA = successes / (completed non-aborted) = 1/2 = 50%.
    """
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-2', 'status': 'aborted', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-3', 'status': 'failed', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['sla'] == 50


def test_list_pipelines_progress_includes_skipped(mocker):
    """Progress: skipped tasks count as complete."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'skipped', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'waiting', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'exec-1', 'status': 'waiting', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['progress'] == 50


def test_list_pipelines_schedule_from_registry(mocker):
    """list_pipelines returns schedule field from registry."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': '', 'schedule': 'cron(0 8 * * ? *)'},
        {'pipeline_name': 'weekly', 'sfn_arn': 'arn:...', 'description': '', 'schedule': 'cron(0 10 ? * MON *)'},
        {'pipeline_name': 'manual', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event()))

    by_name = {p['name']: p for p in resp['pipelines']}
    assert by_name['daily']['schedule'] == 'cron(0 8 * * ? *)'
    assert by_name['weekly']['schedule'] == 'cron(0 10 ? * MON *)'
    assert by_name['manual']['schedule'] == ''


def test_list_pipelines_recent_runs(mocker):
    """Stats include recent_runs sparkline data including in-progress runs."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo([
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-1', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-2', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-2', 'status': 'failed', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-3', 'status': 'success', 'date': TODAY},
        {'pipeline_name': 'daily', 'pipeline_execution': 'run-3', 'status': 'running', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    runs = p['recent_runs']
    assert len(runs) == 3
    statuses = {r['status'] for r in runs}
    assert statuses == {'success', 'failed', 'running'}


def test_list_pipelines_recent_runs_empty_when_no_history(mocker):
    """recent_runs is None when no execution history exists."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:...', 'description': ''},
    ])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mocker.MagicMock())
    from routes.pipelines_list import list_pipelines
    resp = _parse_response(list_pipelines(_make_event({'stats': 'true'})))

    p = resp['pipelines'][0]
    assert p['recent_runs'] is None


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
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'SUCCEEDED'}

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mock_sfn)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    stats = resp['stats']
    assert stats['success'] == 2
    assert stats['stopped'] == 2
    assert stats['skipped'] == 1
    assert stats['total'] == 5


def test_pipeline_status_reconciliation_marks_aborted(mocker):
    """Orphaned waiting tasks become aborted when execution is FAILED."""
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't1', 'status': 'success', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e2', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't2', 'status': 'waiting_delay', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
        {'execution_name': 'e3', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't3', 'status': 'running', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
    ])
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'FAILED'}

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mock_sfn)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    tasks = resp['tasks']
    statuses = {t['task_name']: t['status'] for t in tasks}
    assert statuses['t1'] == 'success'
    assert statuses['t2'] == 'aborted'
    assert statuses['t3'] == 'aborted'
    assert resp['stats']['stopped'] == 2


def test_pipeline_status_sfn_arn_field(mocker):
    """Reconciliation reads sfn_arn (not arn) from registry."""
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't1', 'status': 'waiting', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
    ])
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'ABORTED'}

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mock_sfn)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    mock_sfn.describe_execution.assert_called_once()
    assert resp['tasks'][0]['status'] == 'aborted'


def test_pipeline_status_no_reconciliation_when_running(mocker):
    """No reconciliation when execution is RUNNING."""
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 't1', 'status': 'waiting_delay', 'date': TODAY, 'started_at': '2026-01-01T00:00:00Z'},
    ])
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'RUNNING'}

    mocker.patch('routes.pipelines_list.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_list.pipelines_repo', mock_pipelines)
    mocker.patch('routes.pipelines_list.sfn', mock_sfn)
    from routes.pipelines_list import get_pipeline_status
    resp = _parse_response(get_pipeline_status('daily', _make_event({'date': TODAY})))

    assert resp['tasks'][0]['status'] == 'waiting_delay'


# ============================================================
# get_pipeline_dag tests
# ============================================================

def test_pipeline_dag_empty_edges(mocker):
    """DAG with edges=[] is returned correctly."""
    dag_data = {'nodes': [{'id': 'a', 'name': 'a'}, {'id': 'b', 'name': 'b'}], 'edges': []}
    mock_pipelines = MockRepo([{'pipeline_name': 'parallel', 'dag': json.dumps(dag_data)}])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('parallel', _make_event()))

    assert resp['nodes'] == dag_data['nodes']
    assert resp['edges'] == []


def test_pipeline_dag_with_edges(mocker):
    """DAG with edges is returned with full edge data."""
    dag_data = {'nodes': [{'id': 'a'}, {'id': 'b'}], 'edges': [{'source': 'a', 'acme': 'b'}]}
    mock_pipelines = MockRepo([{'pipeline_name': 'chain', 'dag': json.dumps(dag_data)}])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('chain', _make_event()))

    assert len(resp['edges']) == 1
    assert resp['edges'][0]['source'] == 'a'


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
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('nodag', _make_event({'date': TODAY})))

    assert len(resp['nodes']) == 2
    assert len(resp['edges']) == 1


def test_pipeline_dag_snapshot_preferred_over_registry(mocker):
    """When pipeline_execution has a snapshot, use it instead of registry."""
    registry_dag = {'nodes': [{'id': 'a'}, {'id': 'b'}, {'id': 'd'}], 'edges': []}
    snapshot_dag = {'nodes': [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}], 'edges': []}
    mock_pipelines = MockRepo([{'pipeline_name': 'daily', 'dag': json.dumps(registry_dag)}])
    mock_execs = MockRepo([
        {'execution_name': 'dag_snapshot::exec-old', 'pipeline_name': 'daily',
         'pipeline_execution': 'exec-old', 'dag': json.dumps(snapshot_dag)},
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-old',
         'task_name': 'a', 'status': 'success', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event({
    'pipeline_execution': 'exec-old'
    })))

    node_ids = [n['id'] for n in resp['nodes']]
    assert 'c' in node_ids
    assert 'd' not in node_ids
    assert resp['dag_source'] == 'snapshot'


def test_pipeline_dag_falls_back_to_registry_without_snapshot(mocker):
    """When no snapshot exists for execution, fall back to registry."""
    registry_dag = {'nodes': [{'id': 'a'}, {'id': 'b'}], 'edges': [{'from': 'a', 'to': 'b'}]}
    mock_pipelines = MockRepo([{'pipeline_name': 'daily', 'dag': json.dumps(registry_dag)}])
    mock_execs = MockRepo([
        {'execution_name': 'e1', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1',
         'task_name': 'a', 'status': 'success', 'date': TODAY},
    ])

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event({
    'pipeline_execution': 'exec-1'
    })))

    assert len(resp['nodes']) == 2
    assert resp['dag_source'] == 'registry'


def test_pipeline_dag_no_execution_uses_registry(mocker):
    """Without pipeline_execution parameter, always use registry."""
    registry_dag = {'nodes': [{'id': 'x'}], 'edges': []}
    mock_pipelines = MockRepo([{'pipeline_name': 'daily', 'dag': json.dumps(registry_dag)}])
    mock_execs = MockRepo()

    mocker.patch('routes.pipelines_info.executions_repo', mock_execs)
    mocker.patch('routes.pipelines_info.pipelines_repo', mock_pipelines)
    from routes.pipelines_info import get_pipeline_dag
    resp = _parse_response(get_pipeline_dag('daily', _make_event()))

    assert resp['dag_source'] == 'registry'


# ============================================================
# _reconcile_orphaned_tasks
# ============================================================

def test_reconcile_orphaned_tasks_marks_aborted(mocker):
    """Non-terminal tasks in FAILED execution → aborted."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'FAILED'}
    tasks = [
        {'task_name': 'a', 'status': 'success', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
        {'task_name': 'b', 'status': 'waiting_delay', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
        {'task_name': 'c', 'status': 'running', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
    ]

    mocker.patch('routes.tasks.pipelines_repo', mock_pipelines)
    mocker.patch('routes.tasks.sfn', mock_sfn)
    from routes.tasks import _reconcile_orphaned_tasks
    result = _reconcile_orphaned_tasks(tasks)

    assert result[0]['status'] == 'success'
    assert result[1]['status'] == 'aborted'
    assert result[2]['status'] == 'aborted'


def test_reconcile_all_terminal_no_sfn_call(mocker):
    """All tasks terminal → no SFN API calls."""
    mock_sfn = mocker.MagicMock()
    tasks = [
        {'task_name': 'a', 'status': 'success', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
        {'task_name': 'b', 'status': 'failed', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
    ]

    mocker.patch('routes.tasks.pipelines_repo', MockRepo())
    mocker.patch('routes.tasks.sfn', mock_sfn)
    from routes.tasks import _reconcile_orphaned_tasks
    _reconcile_orphaned_tasks(tasks)

    mock_sfn.describe_execution.assert_not_called()


def test_reconcile_sfn_arn_fallback_to_arn(mocker):
    """Registry with only 'arn' (no sfn_arn) still enables reconciliation."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'FAILED'}
    tasks = [
        {'task_name': 'a', 'status': 'running', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
    ]

    mocker.patch('routes.tasks.pipelines_repo', mock_pipelines)
    mocker.patch('routes.tasks.sfn', mock_sfn)
    from routes.tasks import _reconcile_orphaned_tasks
    result = _reconcile_orphaned_tasks(tasks)

    assert result[0]['status'] == 'aborted'


def test_reconcile_multiple_pipelines_independent(mocker):
    """Tasks from different pipelines reconciled independently."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'p1', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:p1'},
        {'pipeline_name': 'p2', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:p2'},
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.side_effect = lambda executionArn: (
        {'status': 'FAILED'} if ':p1:' in executionArn else {'status': 'RUNNING'}
    )
    tasks = [
        {'task_name': 'a', 'status': 'running', 'pipeline_name': 'p1', 'pipeline_execution': 'exec-1'},
        {'task_name': 'b', 'status': 'running', 'pipeline_name': 'p2', 'pipeline_execution': 'exec-2'},
    ]

    mocker.patch('routes.tasks.pipelines_repo', mock_pipelines)
    mocker.patch('routes.tasks.sfn', mock_sfn)
    from routes.tasks import _reconcile_orphaned_tasks
    result = _reconcile_orphaned_tasks(tasks)

    assert result[0]['status'] == 'aborted'
    assert result[1]['status'] == 'running'


def test_reconcile_running_execution_unchanged(mocker):
    """Tasks in RUNNING execution stay unchanged."""
    mock_pipelines = MockRepo([
        {'pipeline_name': 'daily', 'sfn_arn': 'arn:aws:states:us-east-1:123:stateMachine:daily'}
    ])
    mock_sfn = mocker.MagicMock()
    mock_sfn.describe_execution.return_value = {'status': 'RUNNING'}
    tasks = [
        {'task_name': 'a', 'status': 'waiting_delay', 'pipeline_name': 'daily', 'pipeline_execution': 'exec-1'},
    ]

    mocker.patch('routes.tasks.pipelines_repo', mock_pipelines)
    mocker.patch('routes.tasks.sfn', mock_sfn)
    from routes.tasks import _reconcile_orphaned_tasks
    result = _reconcile_orphaned_tasks(tasks)

    assert result[0]['status'] == 'waiting_delay'
