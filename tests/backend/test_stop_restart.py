"""
Tests for stop_task and restart_task.

stop_task:
- Running → stopped (can be restarted)
- Waiting → aborted (terminal, sends callback, notifies dependents)
- Terminal → 409
- Race condition → 409, no side-effects

restart_task:
- Terminal → restart initiated (SFN helper)
- Non-terminal → 409
- No restart helper ARN → fallback reset
"""

import json
import os
from botocore.exceptions import ClientError

os.environ.setdefault('DYNAMODB_TABLE', 'test-tokens')
os.environ.setdefault('PIPELINES_TABLE', 'test-registry')
os.environ.setdefault('TASK_EVENTS_TABLE', 'test-events')
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')


def _conditional_check_failed():
    return ClientError(
        {'Error': {'Code': 'ConditionalCheckFailedException', 'Message': 'Condition not met'}}, 'UpdateItem'
    )


def _make_item(status='running'):
    return {
        'execution_name': 'extract-2026-02-20-abc12345',
        'task_name': 'extract',
        'status': status,
        'pipeline_name': 'test_pipeline',
        'pipeline_execution': 'arn:aws:states:us-east-1:123:execution:test:exec-1',
        'pipeline_execution_short': 'exec-1',
        'date': '2026-02-20',
        'orchestration_token': 'tok-123',
        'task_run_id': 'extract-2026-02-20-abc12345',
        'wrapper_execution_arn': 'arn:aws:states:us-east-1:123:execution:wrapper:extract-123',
    }


def _make_event(date='2026-02-20'):
    return {'body': json.dumps({'date': date})}


# ============================================================
# stop_task
# ============================================================


def test_stop_running_returns_stopped(mocker):
    """Running task → stopped status (not aborted)."""
    from routes.tasks import stop_task

    item = _make_item(status='running')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mock_stop_exec = mocker.patch('routes.tasks.stop_task_executions')
    mock_record = mocker.patch('routes.tasks.record_manual_decision')
    mock_repo = mocker.patch('routes.tasks.executions_repo')
    mock_repo.update.return_value = {}

    response = stop_task('extract', _make_event())

    assert response['statusCode'] == 200
    body = json.loads(response['body'])
    assert body['status'] == 'stopped'
    mock_stop_exec.assert_called_once()
    mock_record.assert_called_once()


def test_stop_waiting_returns_aborted(mocker):
    """Waiting task → aborted status (terminal), sends callback."""
    from routes.tasks import stop_task

    item = _make_item(status='waiting_decision')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mocker.patch('routes.tasks.stop_task_executions')
    mocker.patch('routes.tasks.record_manual_decision')
    mock_notify = mocker.patch('routes.tasks.notify_dependents_via_sfn', return_value=True)
    mock_repo = mocker.patch('routes.tasks.executions_repo')
    mock_repo.update.return_value = {}
    mock_sfn = mocker.patch('routes.tasks.sfn')

    response = stop_task('extract', _make_event())

    assert response['statusCode'] == 200
    body = json.loads(response['body'])
    assert body['status'] == 'aborted'
    mock_sfn.send_task_failure.assert_called_once()
    mock_notify.assert_called_once()


def test_stop_terminal_returns_409(mocker):
    """Already terminal → 409 conflict."""
    from routes.tasks import stop_task

    item = _make_item(status='success')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))

    response = stop_task('extract', _make_event())

    assert response['statusCode'] == 409
    assert 'terminal' in json.loads(response['body'])['error'].lower()


def test_stop_not_found_returns_404(mocker):
    """Task not found → 404."""
    from routes.tasks import stop_task

    mocker.patch('routes.tasks.resolve_task_item', return_value=(None, None))

    response = stop_task('nonexistent', _make_event())

    assert response['statusCode'] == 404


# ============================================================
# restart_task
# ============================================================


def test_restart_terminal_task_with_helper(mocker):
    """Terminal task + RESTART_HELPER_ARN → starts restart SFN."""
    from routes.tasks import restart_task

    item = _make_item(status='failed')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mock_record = mocker.patch('routes.tasks.record_manual_decision')
    mock_sfn = mocker.patch('routes.tasks.sfn')
    mock_sfn.start_execution.return_value = {'executionArn': 'arn:aws:states:us-east-1:123:execution:restart:r-123'}
    mocker.patch.dict(os.environ, {'RESTART_HELPER_ARN': 'arn:restart-helper'})

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 200
    body = json.loads(response['body'])
    assert 'execution_arn' in body
    mock_sfn.start_execution.assert_called_once()
    mock_record.assert_called_once()


def test_restart_non_terminal_returns_409(mocker):
    """Active (non-restartable) task → 409 conflict."""
    from routes.tasks import restart_task

    item = _make_item(status='running')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 409
    assert 'not in restartable state' in json.loads(response['body'])['error'].lower()


def test_restart_not_found_returns_404(mocker):
    """Task not found → 404."""
    from routes.tasks import restart_task

    mocker.patch('routes.tasks.resolve_task_item', return_value=(None, None))

    response = restart_task('nonexistent', _make_event())

    assert response['statusCode'] == 404


def test_restart_without_helper_arn_returns_500(mocker):
    """RESTART_HELPER_ARN is set automatically by the SAM template. Its absence
    means a broken/partial deploy. The old fallback path could not do the
    two-level stop that the helper does (would leave ghost wrappers alive on
    waiting_decision restarts), so restart_task must now fail fast with a clear
    error instead of attempting a degraded restart."""
    from routes.tasks import restart_task

    item = _make_item(status='failed')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))

    os.environ.pop('RESTART_HELPER_ARN', None)
    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 500
    assert 'RESTART_HELPER_ARN' in json.loads(response['body'])['error']


def test_restart_stopped_task_via_helper(mocker):
    """Regression test — real bug: stop_task's own success message says
    "Use Restart to resume", but restart_task previously only accepted
    TASK_TERMINAL_STATUSES, which deliberately excludes 'stopped' (stop_task
    treats 'stopped' as non-terminal/resumable on purpose). Any user who
    stopped a task and then restarted it, exactly as instructed, got a 409.
    """
    from routes.tasks import restart_task

    item = _make_item(status='stopped')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mocker.patch('routes.tasks.record_manual_decision')
    mock_sfn = mocker.patch('routes.tasks.sfn')
    mock_sfn.start_execution.return_value = {'executionArn': 'arn:aws:states:us-east-1:123:execution:restart:r-123'}
    mocker.patch.dict(os.environ, {'RESTART_HELPER_ARN': 'arn:restart-helper'})

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 200
    mock_sfn.start_execution.assert_called_once()




def test_restart_waiting_decision_task_via_helper(mocker):
    """§9 final step: Restart now works directly from waiting_decision —
    no need to Stop first. Goes through restart_task_helper's Stop_Old_
    Wrapper (a hard states:StopExecution kill, not send_task_success/
    failure), so unlike Stop it never triggers notify_dependents_via_sfn's
    unconditional, immediate downstream notification (SPIKE_TASK_ACTIONS_
    DATA_LIFECYCLE.md §8/§9). Safe now that both §9 prerequisites are in
    place: the correct field name (run_task_helper_arn) so Stop_Old_Wrapper
    can actually find and kill the still-live wrapper, and the attempt-keyed
    ConditionExpression guard so a surviving ghost can never corrupt the new
    attempt's state even if the kill somehow fails."""
    from routes.tasks import restart_task

    item = _make_item(status='waiting_decision')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mock_record = mocker.patch('routes.tasks.record_manual_decision')
    mock_sfn = mocker.patch('routes.tasks.sfn')
    mock_sfn.start_execution.return_value = {'executionArn': 'arn:aws:states:us-east-1:123:execution:restart:r-123'}
    mocker.patch.dict(os.environ, {'RESTART_HELPER_ARN': 'arn:restart-helper'})

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 200
    mock_sfn.start_execution.assert_called_once()
    mock_record.assert_called_once()


def test_restart_still_rejects_genuinely_active_running_task(mocker):
    """Only waiting_decision was added — a genuinely running task (no
    decision pending, still actively executing) must still be rejected;
    Restart is not a general-purpose way to interrupt any active task."""
    from routes.tasks import restart_task

    item = _make_item(status='running')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 409
    assert 'not in restartable state' in json.loads(response['body'])['error'].lower()


def test_restart_looks_up_and_passes_task_config_and_outlets(mocker):
    """task_config/outlets are deploy-time DAG properties never stored on
    the per-execution record — restart_task must look them up from the
    registry and pass them explicitly to restart_task_helper, or a
    restarted task with retries/worker-type/outlets configured would
    silently lose that configuration (previously always reconstructed as
    empty by Start_New_Wrapper, since the fields it tried to read never
    existed on the item)."""
    from routes.tasks import restart_task

    item = _make_item(status='failed')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mocker.patch('routes.tasks.record_manual_decision')
    mock_sfn = mocker.patch('routes.tasks.sfn')
    mock_sfn.start_execution.return_value = {'executionArn': 'arn:aws:states:us-east-1:123:execution:restart:r-123'}
    mocker.patch.dict(os.environ, {'RESTART_HELPER_ARN': 'arn:restart-helper'})
    mocker.patch('routes.tasks.pipelines_repo.get', return_value={
        'pipeline_name': 'test_pipeline',
        'dag': json.dumps({
            'nodes': [{
                'id': 'extract',
                'task_config': {'retries': 3, 'retry_delay': 120},
                'outlets': [{'name': 'raw/sales'}],
            }],
            'edges': [],
        }),
    })

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 200
    call_kwargs = mock_sfn.start_execution.call_args.kwargs
    sent_input = json.loads(call_kwargs['input'])
    assert sent_input['task_config'] == {'retries': 3, 'retry_delay': 120}
    assert sent_input['outlets'] == [{'name': 'raw/sales'}]


def test_restart_without_registry_entry_passes_empty_task_config_and_outlets(mocker):
    """No registry entry found (e.g. registry read failure) → graceful
    empty defaults, not a crash — restart still proceeds."""
    from routes.tasks import restart_task

    item = _make_item(status='failed')
    mocker.patch('routes.tasks.resolve_task_item', return_value=(item, item['execution_name']))
    mocker.patch('routes.tasks.record_manual_decision')
    mock_sfn = mocker.patch('routes.tasks.sfn')
    mock_sfn.start_execution.return_value = {'executionArn': 'arn:aws:states:us-east-1:123:execution:restart:r-123'}
    mocker.patch.dict(os.environ, {'RESTART_HELPER_ARN': 'arn:restart-helper'})
    mocker.patch('routes.tasks.pipelines_repo.get', return_value=None)

    response = restart_task('extract', _make_event())

    assert response['statusCode'] == 200
    call_kwargs = mock_sfn.start_execution.call_args.kwargs
    sent_input = json.loads(call_kwargs['input'])
    assert sent_input['task_config'] == {}
    assert sent_input['outlets'] == []
