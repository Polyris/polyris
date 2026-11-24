"""The bell (`/api/notifications`) on a moto tokens table (ADR #137 §6, D92).

It reads the attention index only: every `failed` entry of its window, by when the
task failed, and every `decision` entry, by its deadline, whatever the date of the run.
The rows carry the entries their writers give them (`row_write.entry_after`, the one
function every writer of a task row takes its entry from), so the bell reads what the
engine, `run_task` and the console write. One notification per run, a decision request
over a failure; the open requests first, then the rest newest first.
"""
import json
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from polyris.coordination import deadlines, row_write, task_row
from polyris.coordination.records import warning_item
from polyris.coordination.times import format_utc
from tests.runs_kit import NOW, freeze, open_tables


@pytest.fixture
def table(mocker):
    with mock_aws():
        table = open_tables(mocker, boto3.resource('dynamodb', region_name='us-east-1'))
        freeze(mocker, 'routes.notifications')
        yield table


def put_row(table, status, *, run='orders-run-2026-11-22-abcd1234', task='load', date='2026-11-22',
            failed_ago=timedelta(minutes=5), deadline=None, error='boom'):
    """A task row with the attention entry its status gives it (row_write.entry_after)."""
    key = task_row.key(task, date, run)
    item = {task_row.TABLE_KEY: key, task_row.TASK_NAME: task, task_row.PIPELINE: run.split('-run-')[0],
            task_row.PIPELINE_EXECUTION: run, task_row.DATE: date, task_row.TASK_DATE: date,
            task_row.STATUS: status, task_row.ERROR: error}
    entry = row_write.entry_after(status, deadline, NOW - failed_ago, key)
    if entry is not None:
        item['attention'], item['attention_at'] = entry
    table.put_item(Item=item)


def bell(**query):
    from routes import notifications

    response = notifications.get_notifications({'queryStringParameters': {'hours': '48', 'limit': '10', **query}})
    assert response['statusCode'] == 200
    return json.loads(response['body'])['notifications']


def test_a_failure_shows_by_when_it_failed_whatever_the_date_of_its_run(table):
    """D92: a wait_for task failing on its 7th day, a backfill of an old date."""
    put_row(table, 'failed', run='orders-run-2026-10-01-abcd1234', date='2026-10-01', failed_ago=timedelta(hours=3))
    [n] = bell()
    assert (n['type'], n['task_name'], n['error'], n['date']) == ('failure', 'load', 'boom', '2026-10-01')
    assert (n['finished_at'], n['time_ago']) == (format_utc(NOW - timedelta(hours=3)), '3h ago')


def test_a_failure_older_than_the_window_does_not_show(table):
    put_row(table, 'failed', failed_ago=timedelta(hours=49))
    assert bell() == []
    assert len(bell(hours='72')) == 1


def test_every_open_decision_request_shows_with_its_deadline_whatever_its_age(table):
    """§6: the bell reads every decision entry; in a pause its deadline is far in the future, and shows none."""
    put_row(table, 'waiting_decision', run='orders-run-2026-09-01-abcd1234', date='2026-09-01',
            deadline=NOW + timedelta(hours=4))
    put_row(table, 'waiting_decision', run='billing-run-2026-11-22-bbbbbbbb', deadline=deadlines.FAR_FUTURE)
    first, second = bell()
    assert (first['type'], first['pipeline_name'], first['deadline']) == \
        ('decision_required', 'orders', format_utc(NOW + timedelta(hours=4)))
    assert (second['pipeline_name'], second['deadline']) == ('billing', None)


def test_one_notification_per_run_and_a_decision_request_outranks_a_failure(table):
    for i in range(3):
        put_row(table, 'failed', task=f'extract_{i}')
    put_row(table, 'waiting_decision', task='decide', deadline=NOW + timedelta(hours=1))
    put_row(table, 'failed', run='billing-run-2026-11-22-bbbbbbbb')
    shown = [(n['pipeline_name'], n['type'], n['task_name']) for n in bell()]
    assert shown == [('orders', 'decision_required', 'decide'), ('billing', 'failure', 'load')]


def test_each_notification_names_its_event_so_a_later_request_of_the_same_run_is_new(table):
    """Review of TR-6d, B-2: the bell keeps "read" and "cleared" by id, and a 1.3 run goes on after a failure. An
    id per run hid a later decision request of a run whose failure was read or cleared."""
    put_row(table, 'failed', task='extract')
    [failure] = bell()
    assert [n['id'] for n in bell()] == [failure['id']]                      # one event keeps its id
    put_row(table, 'waiting_decision', task='load', deadline=NOW + timedelta(hours=5))
    [first] = bell()
    assert first['type'] == 'decision_required' and first['id'] != failure['id']
    # A Restart of the task, which fails again and asks for a new decision: the row's new entry, a new id.
    put_row(table, 'waiting_decision', task='load', deadline=NOW + timedelta(hours=6))
    [again] = bell()
    assert again['type'] == 'decision_required' and again['id'] not in (first['id'], failure['id'])


def test_the_open_requests_come_first_the_most_urgent_first_then_the_rest_newest_first(table):
    put_row(table, 'failed', run='a-run-2026-11-22-aaaaaaaa', failed_ago=timedelta(minutes=1))
    put_row(table, 'failed', run='b-run-2026-11-22-bbbbbbbb', failed_ago=timedelta(hours=2))
    put_row(table, 'waiting_decision', run='c-run-2026-11-22-cccccccc', deadline=NOW + timedelta(hours=3))
    put_row(table, 'waiting_decision', run='d-run-2026-11-22-dddddddd', deadline=NOW + timedelta(minutes=30))
    assert [n['pipeline_name'] for n in bell()] == ['d', 'c', 'a', 'b']
    assert [n['pipeline_name'] for n in bell(limit='3')] == ['d', 'c', 'a']


def test_a_row_without_such_an_entry_shows_nothing(table):
    """A Restart, a Skip, a Mark Successful or a Stop removes the entry (§3); a row of a 1.2 run never had one."""
    put_row(table, 'stopped')
    put_row(table, 'upstream_failed', task='report')
    table.put_item(Item={'execution_name': 'load-2026-11-22-legacy', 'task_name': 'load', 'pipeline_name': 'orders',
                         'pipeline_execution': 'orders-2026-11-22-legacy', 'date': '2026-11-22', 'status': 'failed',
                         'finished_at': format_utc(NOW - timedelta(minutes=5))})
    assert bell() == []


def test_the_warnings_of_the_window_show_beside_them(table):
    table.put_item(Item=warning_item(kind='coordination', execution_name='orders-run-2026-11-22-abcd1234',
                                     task_name='', pipeline_name='orders',
                                     pipeline_execution='orders-run-2026-11-22-abcd1234', task_date='2026-11-22',
                                     error='a pass failed', now=NOW - timedelta(minutes=10)))
    put_row(table, 'failed', run='billing-run-2026-11-22-bbbbbbbb')
    assert [(n['type'], n['pipeline_name']) for n in bell()] == [('failure', 'billing'), ('warning', 'orders')]
