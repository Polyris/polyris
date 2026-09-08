"""Tests for routes.pipelines_list._fill_triggered_by.

Verifies that triggered_by is back-filled from the base table on executions
that came from a GSI projection that doesn't carry the field.
"""
import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def pl(mocker):
    """Return (module, mock_executions_repo) with executions_repo patched."""
    from routes import pipelines_list as m
    mock_repo = mocker.MagicMock()
    mocker.patch.object(m, 'executions_repo', mock_repo)
    return m, mock_repo


class TestFillTriggeredBy:
    def test_fills_triggered_by_from_batch_result(self, pl):
        m, repo = pl
        repo.batch_get_triggered_by.return_value = {'task-a': 'console_run'}
        runs = [{'pipeline_execution': 'pe-1', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        assert runs[0]['triggered_by'] == 'console_run'

    def test_pops_sample_exec_name_from_every_execution(self, pl):
        """_sample_exec_name must never leak into the API response."""
        m, repo = pl
        repo.batch_get_triggered_by.return_value = {'task-a': 'schedule'}
        runs = [{'pipeline_execution': 'pe-1', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        assert '_sample_exec_name' not in runs[0]

    def test_pops_sample_exec_name_even_when_triggered_by_already_set(self, pl):
        """Already-set triggered_by: _sample_exec_name still popped, no batch call."""
        m, repo = pl
        runs = [{'pipeline_execution': 'pe-1', 'triggered_by': 'schedule', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        assert '_sample_exec_name' not in runs[0]
        assert runs[0]['triggered_by'] == 'schedule'
        repo.batch_get_triggered_by.assert_not_called()

    def test_empty_sample_name_skips_batch_call(self, pl):
        m, repo = pl
        runs = [{'pipeline_execution': 'pe-1', '_sample_exec_name': ''}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        repo.batch_get_triggered_by.assert_not_called()

    def test_none_from_base_table_sets_none(self, pl):
        """Row exists but triggered_by absent (old run before feature) → None."""
        m, repo = pl
        repo.batch_get_triggered_by.return_value = {'task-a': None}
        runs = [{'pipeline_execution': 'pe-1', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        assert runs[0]['triggered_by'] is None

    def test_client_error_leaves_triggered_by_unset(self, pl):
        """DDB error must not raise; triggered_by stays absent (display-only)."""
        m, repo = pl
        repo.batch_get_triggered_by.side_effect = ClientError(
            {'Error': {'Code': 'ProvisionedThroughputExceededException', 'Message': ''}},
            'BatchGetItem',
        )
        runs = [{'pipeline_execution': 'pe-1', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs, id_field='pipeline_execution')  # must not raise
        assert runs[0].get('triggered_by') is None

    def test_default_id_field_is_execution_id(self, pl):
        """get_pipeline_executions uses default id_field='execution_id'."""
        m, repo = pl
        repo.batch_get_triggered_by.return_value = {'task-a': 'asset'}
        runs = [{'execution_id': 'exe-1', '_sample_exec_name': 'task-a'}]
        m._fill_triggered_by(runs)
        assert runs[0]['triggered_by'] == 'asset'

    def test_two_executions_sharing_sample_both_get_filled(self, pl):
        """Two pipeline executions with the same sample → one batch call, both filled."""
        m, repo = pl
        repo.batch_get_triggered_by.return_value = {'task-a': 'backfill'}
        runs = [
            {'pipeline_execution': 'pe-1', '_sample_exec_name': 'task-a'},
            {'pipeline_execution': 'pe-2', '_sample_exec_name': 'task-a'},
        ]
        m._fill_triggered_by(runs, id_field='pipeline_execution')
        assert repo.batch_get_triggered_by.call_count == 1
        assert runs[0]['triggered_by'] == 'backfill'
        assert runs[1]['triggered_by'] == 'backfill'
