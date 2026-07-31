"""
Tests for query_subscriptions Lambda.

v0.79.3 (ADR #75) — migrated to DAL repo pattern. Tests now mock
`subscriptions_repo.list_for_dependency` instead of patching boto3
internals via `dynamodb.Table().query()` chains.

Note: the DAL package in this Lambda is named `qs_dal/` (not `dal/`) to
avoid colliding with console_api's `dal` package on sys.path when both
are loaded for the backend test suite.
"""

import os
import sys

os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('SUBSCRIPTIONS_TABLE', 'test-subscriptions')

# Ensure query_subscriptions dir is importable for `from qs_dal import ...`
QS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'sam', 'lambdas', 'query_subscriptions',
)
if QS_DIR not in sys.path:
    sys.path.insert(0, QS_DIR)

import sam.lambdas.query_subscriptions.index as qs_module


def _make_subscriber(task_name, wait_token='tok-1'):
    return {
        'dependency_key': 'extract-abc12345',
        'subscriber': f'{task_name}-abc12345',
        'wait_token': wait_token,
    }


def test_happy_path_returns_subscribers(mocker):
    mocker.patch.object(qs_module.subscriptions_repo, 'list_for_dependency',
                        return_value=[_make_subscriber('transform'),
                                      _make_subscriber('load', 'tok-2')])
    result = qs_module.handler({
        'task_name': 'extract',
        'pipeline_execution_short': 'abc12345',
    }, {})
    assert len(result['subscribers']) == 2
    assert result['subscribers'][0]['subscriber'] == 'transform-abc12345'


def test_missing_task_name_returns_empty():
    result = qs_module.handler({'pipeline_execution_short': 'abc12345'}, {})
    assert result['subscribers'] == []


def test_missing_pipeline_execution_short_returns_empty():
    result = qs_module.handler({'task_name': 'extract'}, {})
    assert result['subscribers'] == []


def test_no_subscribers_returns_empty(mocker):
    mocker.patch.object(qs_module.subscriptions_repo, 'list_for_dependency',
                        return_value=[])
    result = qs_module.handler({
        'task_name': 'extract',
        'pipeline_execution_short': 'abc12345',
    }, {})
    assert result['subscribers'] == []


def test_pagination_handled_inside_repo(mocker):
    """v0.79.3 — pagination loop now lives in DAL. From the handler's
    perspective the repo returns the final aggregated list."""
    items = [_make_subscriber(f'task-{i}') for i in range(5)]
    mocker.patch.object(qs_module.subscriptions_repo, 'list_for_dependency',
                        return_value=items)
    result = qs_module.handler({
        'task_name': 'extract',
        'pipeline_execution_short': 'abc12345',
    }, {})
    assert len(result['subscribers']) == 5


def test_ddb_error_returns_empty_with_error(mocker):
    mocker.patch.object(qs_module.subscriptions_repo, 'list_for_dependency',
                        side_effect=Exception('Connection refused'))
    result = qs_module.handler({
        'task_name': 'extract',
        'pipeline_execution_short': 'abc12345',
    }, {})
    assert result['subscribers'] == []
    assert 'error' in result


def test_access_denied_reraises(mocker):
    """AccessDenied propagates for SFN Retry to catch."""
    mocker.patch.object(qs_module.subscriptions_repo, 'list_for_dependency',
                        side_effect=Exception('AccessDeniedException: nope'))
    import pytest
    with pytest.raises(Exception, match='AccessDenied'):
        qs_module.handler({
            'task_name': 'extract',
            'pipeline_execution_short': 'abc12345',
        }, {})
