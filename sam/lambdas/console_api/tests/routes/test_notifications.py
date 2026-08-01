"""Unit tests for routes.notifications.get_notifications.

Covers the two attention states (ADR #107): a plain task failure ('failure') and a
task paused awaiting a manual decision ('decision_required'), deduplicated to one
notification per run, with decision-required taking priority when both are present.
executions_repo.query_by_date_raw is mocked (the external data boundary).
"""
import json
from datetime import datetime, timezone, timedelta


def _now_iso(minutes_ago=5):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def _task_row(pipeline='p1', pe='e1', task='t1', status='failed',
              finished=None, running=None, error='boom'):
    return {
        'pipeline_name': pipeline,
        'pipeline_execution': pe,
        'task_name': task,
        'status': status,
        'finished_at': finished if finished is not None else _now_iso(),
        'running_at': running if running is not None else _now_iso(),
        'error': error,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
    }


def _ddb_response(rows):
    return {'Items': list(rows), 'Count': len(rows), 'LastEvaluatedKey': None}


def _call(mocker, rows, hours='24', limit='10'):
    from routes import notifications
    mocker.patch.object(notifications.executions_repo, 'query_by_date_raw',
                        return_value=_ddb_response(rows))
    # Isolate from backfill notifications (tested separately).
    mocker.patch.object(notifications, '_backfill_terminal_notifications',
                        return_value=[])
    event = {'queryStringParameters': {'hours': hours, 'limit': limit}}
    resp = notifications.get_notifications(event)
    return resp, json.loads(resp['body'])


class TestNotificationStates:
    def test_failed_task_yields_failure(self, mocker):
        _resp, body = _call(mocker, [_task_row(status='failed', task='extract')])
        assert body['count'] == 1
        n = body['notifications'][0]
        assert n['type'] == 'failure'
        assert n['task_name'] == 'extract'
        assert n['error'] == 'boom'

    def test_waiting_decision_yields_decision_required(self, mocker):
        # Paused task has no finished_at; it must still surface (anchored on running_at).
        row = _task_row(status='waiting_decision', task='decide', finished='', error='')
        _resp, body = _call(mocker, [row])
        assert body['count'] == 1
        n = body['notifications'][0]
        assert n['type'] == 'decision_required'
        assert n['task_name'] == 'decide'

    def test_decision_outranks_failure_in_same_run(self, mocker):
        rows = [
            _task_row(pe='e9', task='failed_step', status='failed'),
            _task_row(pe='e9', task='decision_step', status='waiting_decision', finished=''),
        ]
        _resp, body = _call(mocker, rows)
        assert body['count'] == 1
        n = body['notifications'][0]
        assert n['type'] == 'decision_required'
        assert n['task_name'] == 'decision_step'

    def test_many_failed_tasks_dedupe_to_one_per_run(self, mocker):
        rows = [_task_row(pe='e5', task=f't{i}', status='failed') for i in range(3)]
        _resp, body = _call(mocker, rows)
        assert body['count'] == 1

    def test_distinct_runs_yield_distinct_notifications(self, mocker):
        rows = [
            _task_row(pipeline='pa', pe='e1', status='failed'),
            _task_row(pipeline='pb', pe='e2', status='waiting_decision', finished=''),
        ]
        _resp, body = _call(mocker, rows)
        assert body['count'] == 2
        types = {n['type'] for n in body['notifications']}
        assert types == {'failure', 'decision_required'}
