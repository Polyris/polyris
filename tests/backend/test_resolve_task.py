"""
Tests for resolve_task_item() — the unified task resolver.

Handles two formats:
- Full execution_name (direct primary key lookup)
- Task name only (GSI lookup by date with pagination)

Critical for all task actions: skip, fail, mark_success, stop, restart.
"""

import os

os.environ.setdefault('DYNAMODB_TABLE', 'test-tokens')
os.environ.setdefault('PIPELINES_TABLE', 'test-registry')
os.environ.setdefault('TASK_EVENTS_TABLE', 'test-events')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


def _make_item(task_name, date='2026-02-20', status='running', short_id='abc12345'):
    """Create a task item with consistent execution_name format."""
    execution_name = f'{task_name}-{date}-{short_id}'
    return {
        'execution_name': execution_name,
        'task_name': task_name,
        'status': status,
        'date': date,
        'started_at': f'{date}T10:00:00Z',
        'pipeline_execution': f'pipe-{date}-{short_id}',
    }


# ============================================================
# Direct lookup (execution_name format)
# ============================================================

def test_resolve_direct_hit(mocker):
    """Full execution_name → direct primary key lookup succeeds."""
    item = _make_item('extract')
    mock_repo = mocker.MagicMock()
    mock_repo.get.return_value = item

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, result_name = resolve_task_item(
        'extract-2026-02-20-abc12345', '2026-02-20'
    )

    assert result_item == item
    assert result_name == 'extract-2026-02-20-abc12345'
    mock_repo.get.assert_called_once_with('extract-2026-02-20-abc12345')


def test_resolve_direct_miss_falls_to_gsi(mocker):
    """execution_name format but not found → falls through to GSI lookup."""
    mock_repo = mocker.MagicMock()
    mock_repo.get.return_value = None

    found_item = _make_item('extract', short_id='xyz99999')
    mock_repo.query_by_date_raw.return_value = {
        'Items': [{
            'execution_name': found_item['execution_name'],
            'task_name': 'extract',
            'started_at': '2026-02-20T10:00:00Z',
            'pipeline_execution': 'pipe-1',
        }],
    }
    mock_repo.get.side_effect = [None, found_item]

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, result_name = resolve_task_item(
        'extract-2026-02-20-abc12345', '2026-02-20'
    )

    assert result_item == found_item


# ============================================================
# GSI lookup (task_name format)
# ============================================================

def test_resolve_gsi_lookup_by_task_name(mocker):
    """Plain task name → GSI lookup by date, returns most recent."""
    item_old = _make_item('extract', short_id='old11111')
    item_new = _make_item('extract', short_id='new22222')

    mock_repo = mocker.MagicMock()
    mock_repo.query_by_date_raw.return_value = {
        'Items': [
            {'execution_name': item_old['execution_name'], 'task_name': 'extract',
             'started_at': '2026-02-20T08:00:00Z', 'pipeline_execution': 'p1'},
            {'execution_name': item_new['execution_name'], 'task_name': 'extract',
             'started_at': '2026-02-20T12:00:00Z', 'pipeline_execution': 'p2'},
        ],
    }
    mock_repo.get.return_value = item_new

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, result_name = resolve_task_item('extract', '2026-02-20')

    assert result_item == item_new
    assert result_name == item_new['execution_name']


def test_resolve_gsi_with_pipeline_execution_filter(mocker):
    """GSI lookup filters by pipeline_execution when provided."""
    mock_repo = mocker.MagicMock()
    target_item = _make_item('extract', short_id='target11')

    mock_repo.query_by_date_raw.return_value = {
        'Items': [
            {'execution_name': target_item['execution_name'], 'task_name': 'extract',
             'started_at': '2026-02-20T10:00:00Z', 'pipeline_execution': 'exec-specific'},
        ],
    }
    mock_repo.get.return_value = target_item

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, _ = resolve_task_item('extract', '2026-02-20', 'exec-specific')

    assert result_item == target_item


# ============================================================
# Not found
# ============================================================

def test_resolve_not_found_returns_none(mocker):
    """Task not found → returns (None, None)."""
    mock_repo = mocker.MagicMock()
    mock_repo.get.return_value = None
    mock_repo.query_by_date_raw.return_value = {'Items': []}

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, result_name = resolve_task_item('nonexistent', '2026-02-20')

    assert result_item is None
    assert result_name is None


def test_resolve_gsi_pagination_empty_pages(mocker):
    """GSI returns empty pages before finding item on later page."""
    target_item = _make_item('extract', short_id='found123')

    call_count = 0
    def mock_query(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {'Items': [], 'LastEvaluatedKey': {'pk': 'something'}}
        return {
            'Items': [
                {'execution_name': target_item['execution_name'], 'task_name': 'extract',
                 'started_at': '2026-02-20T10:00:00Z', 'pipeline_execution': 'p1'},
            ],
        }

    mock_repo = mocker.MagicMock()
    mock_repo.query_by_date_raw.side_effect = mock_query
    mock_repo.get.return_value = target_item

    mocker.patch('routes.tasks.executions_repo', mock_repo)
    from routes.tasks import resolve_task_item
    result_item, _ = resolve_task_item('extract', '2026-02-20')

    assert result_item == target_item
    assert call_count == 2
