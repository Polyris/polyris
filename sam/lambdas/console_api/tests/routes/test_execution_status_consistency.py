"""Cross-endpoint execution-status consistency (ADR #112).

The bug that motivated ADR #112: the same stopped execution showed 'aborted' on the
/runs feed (get_all_runs) but 'failed' in the execution-history dropdown
(get_pipeline_executions), because each endpoint hand-derived the status from its task
statuses with drifted rules. Both now share polyris' derive_execution_status; this test
locks it — identical task rows must yield an identical execution status from both
endpoints. No test previously asserted this, which is why the drift went unnoticed.

pytest-mock (ADR #26); mock only at the repo boundary. SFN reconciliation is disabled by
returning no pipeline ARN, so the pure DynamoDB derivation is what's compared.
"""
import json

import pytest


def _task_rows(statuses, pe='p-2024-01-15-abc', pipeline='test-pipeline', date='2024-01-15'):
    """One execution's task rows (same pipeline_execution, one row per task status)."""
    return [
        {
            'execution_name': f'{pipeline}-task{i}-{date}',
            'pipeline_execution': pe,
            'pipeline_execution_short': pe[-8:],
            'pipeline_name': pipeline,
            'status': s,
            'date': date,
            'started_at': '2024-01-15T10:00:00Z',
            'finished_at': '2024-01-15T10:05:00Z',
        }
        for i, s in enumerate(statuses)
    ]


def _runs_status(mocker, rows, date='2024-01-15'):
    from routes import executions
    mocker.patch.object(executions.executions_repo, 'query_runs_by_date', return_value=list(rows))
    mocker.patch.object(executions.backfills_repo, 'list_recent', return_value=[])
    mocker.patch.object(executions.pipelines_repo, 'get', return_value=None)  # no ARN → no reconcile
    resp = executions.get_all_runs({'queryStringParameters': {'date': date}})
    body = json.loads(resp['body'])
    execs = [r for r in body['runs'] if r['kind'] == 'execution']
    assert len(execs) == 1, execs
    return execs[0]['status']


def _dropdown_status(mocker, rows, date='2024-01-15', pipeline='test-pipeline'):
    from routes import pipelines_list
    mocker.patch.object(pipelines_list.executions_repo, 'query_runs_by_date', return_value=list(rows))
    mocker.patch.object(pipelines_list.pipelines_repo, 'get', return_value=None)  # no ARN → no reconcile
    resp = pipelines_list.get_pipeline_executions(pipeline, {'queryStringParameters': {'date': date}})
    body = json.loads(resp['body'])
    execs = body['executions']
    assert len(execs) == 1, execs
    return execs[0]['status']


# (task statuses, expected canonical execution status)
CASES = [
    (['success'], 'success'),
    (['success', 'skipped'], 'success'),
    (['success', 'failed'], 'failed'),
    (['failed'], 'failed'),
    (['aborted'], 'aborted'),
    (['stopped'], 'aborted'),
    (['upstream_failed'], 'aborted'),
    (['success', 'stopped'], 'aborted'),            # the reported bug: a stopped run
    (['success', 'aborted', 'stopped'], 'aborted'),
    (['failed', 'aborted'], 'failed'),              # a genuine failure outranks the stop
    (['running'], 'running'),
    (['running', 'success'], 'running'),
]


@pytest.mark.parametrize("statuses,expected", CASES)
def test_runs_and_dropdown_agree(mocker, statuses, expected):
    rows = _task_rows(statuses)
    runs_status = _runs_status(mocker, rows)
    dropdown_status = _dropdown_status(mocker, rows)
    assert runs_status == expected, f"/runs derived {runs_status}, expected {expected}"
    assert dropdown_status == expected, f"dropdown derived {dropdown_status}, expected {expected}"
    assert runs_status == dropdown_status


class TestPipelineStatusReportsTheRealPipeline:
    """`?pipeline_execution=` looks rows up by execution id alone, so it can return
    another pipeline's tasks. The route used to echo the *requested* name back, which
    made the response agree with the question no matter what it returned — and the
    console's stale-response guard (`statusData.pipeline_name !== pipelineName`) checks
    exactly that, so it could never fire. A guard that cannot fire is not a guard.
    """

    def _rows(self, pipeline='alpha'):
        return [{'execution_name': 'extract-1', 'task_name': 'extract',
                 'pipeline_name': pipeline, 'pipeline_execution': f'{pipeline}-run-1',
                 'status': 'success', 'date': '2026-07-17',
                 'started_at': '2026-07-17T09:00:00.000Z'}]

    def _status(self, mocker, rows, asked_for):
        from routes import pipelines_list
        mocker.patch.object(pipelines_list.executions_repo,
                            'query_by_pipeline_execution', return_value=rows)
        mocker.patch.object(pipelines_list.pipelines_repo, 'get', return_value=None)
        resp = pipelines_list.get_pipeline_status(
            asked_for, {'queryStringParameters': {'name': asked_for,
                                                  'pipeline_execution': 'alpha-run-1'}})
        return json.loads(resp['body'])

    def test_says_which_pipeline_the_tasks_are_from(self, mocker):
        body = self._status(mocker, self._rows('alpha'), asked_for='beta')
        assert body['pipeline_name'] == 'alpha', \
            "echoing the request back is what blinded the console's guard"

    def test_agrees_with_the_request_when_the_rows_do(self, mocker):
        body = self._status(mocker, self._rows('alpha'), asked_for='alpha')
        assert body['pipeline_name'] == 'alpha'

    def test_falls_back_to_the_request_when_there_are_no_rows(self, mocker):
        """Nothing to read the truth off — the question is the only answer available."""
        body = self._status(mocker, [], asked_for='beta')
        assert body['pipeline_name'] == 'beta'

    def test_internal_rows_do_not_decide_it(self, mocker):
        """_notify_warn_ and friends are skipped everywhere else; they don't get to
        name the pipeline either."""
        rows = [{'execution_name': '_notify_warn_x', 'pipeline_name': 'noise',
                 'date': '2026-07-17'}] + self._rows('alpha')
        body = self._status(mocker, rows, asked_for='beta')
        assert body['pipeline_name'] == 'alpha'
