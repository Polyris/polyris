"""
Tests for stop_task and restart_task.

stop_task (its transaction: sam/lambdas/console_api/tests/routes/test_task_actions.py):
- Not found → 404

restart_task (its transaction: sam/lambdas/console_api/tests/routes/test_task_actions.py):
- Not found → 404
"""

import json
import os

os.environ.setdefault('DYNAMODB_TABLE', 'test-tokens')
os.environ.setdefault('PIPELINES_TABLE', 'test-registry')
os.environ.setdefault('TASK_EVENTS_TABLE', 'test-events')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


def _make_event(date='2026-02-20'):
    return {'body': json.dumps({'date': date})}


# ============================================================
# stop_task
# ============================================================


def test_stop_not_found_returns_404(mocker):
    """Task not found → 404."""
    from routes.tasks import stop_task

    mocker.patch('routes.tasks.resolve_task_item', return_value=(None, None))

    response = stop_task('nonexistent', _make_event())

    assert response['statusCode'] == 404


# ============================================================
# restart_task
# ============================================================


def test_restart_not_found_returns_404(mocker):
    """Task not found → 404."""
    from routes.tasks import restart_task

    mocker.patch('routes.tasks.resolve_task_item', return_value=(None, None))

    response = restart_task('nonexistent', _make_event())

    assert response['statusCode'] == 404
