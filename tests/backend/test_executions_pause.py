"""Unit tests for routes.executions.pause_execution.

Added in v0.77.1 cleanup to lock in behavior after removing the dead
`items = executions_repo.query_by_pipeline_execution(...)` line that was
wasting RRU without using the result (CLAUDE.md #1 violation).

The test surface is small because the function itself is small:
  - Happy path writes a `_pause_<exec>` record to pipeline-tokens.
  - The function returns 200 with a confirmation payload.
  - DDB failures degrade to a 500 with a logged error.

Conventions per ADR #26: pytest-mock `mocker` fixture, no
`unittest.mock.patch`.
"""

import json

import pytest


@pytest.fixture
def execution_module(mocker):
    """Patch executions_repo so we can assert on the writes."""
    from routes import executions as executions_module

    executions_repo = mocker.MagicMock()
    executions_repo.put.return_value = None
    mocker.patch.object(executions_module, 'executions_repo', executions_repo)

    return {'executions_repo': executions_repo}


def _body(resp):
    return json.loads(resp['body'])


class TestPauseExecution:
    def test_writes_pause_record_with_correct_key(self, execution_module):
        """The `_pause_<exec>` key prefix is what
        `pipelines_repo.is_paused()` looks for — typo here would silently
        break the pause feature."""
        from routes.executions import pause_execution

        resp = pause_execution('myexec-2026-05-19', {})

        assert resp['statusCode'] == 200
        execution_module['executions_repo'].put.assert_called_once()
        record = execution_module['executions_repo'].put.call_args[0][0]
        assert record['execution_name'] == '_pause_myexec-2026-05-19'
        assert record['paused'] is True
        assert record['pipeline_execution'] == 'myexec-2026-05-19'
        assert 'paused_at' in record  # ISO timestamp from datetime.now

    def test_response_body_carries_confirmation(self, execution_module):
        from routes.executions import pause_execution

        body = _body(pause_execution('xyz', {}))
        assert body['paused'] is True
        assert body['pipeline_execution'] == 'xyz'
        assert 'paused' in body['message'].lower()

    def test_ddb_failure_returns_500(self, execution_module):
        """A put() failure (network, throttling, IAM) shouldn't crash the
        Lambda — we degrade to 500 and log."""
        from routes.executions import pause_execution

        execution_module['executions_repo'].put.side_effect = RuntimeError('DDB down')

        resp = pause_execution('xyz', {})
        assert resp['statusCode'] == 500
        assert 'Failed to pause' in _body(resp)['error']

    def test_no_redundant_query(self, execution_module):
        """v0.77.1 cleanup removed a `query_by_pipeline_execution` call
        whose result was never used (wasted RRU). Lock that out — if a
        future refactor accidentally restores it, this test fails."""
        from routes.executions import pause_execution

        pause_execution('xyz', {})
        # The cleanup removed the only such call from this function.
        # Resume / extend_pause / etc. still use it; this test scopes
        # only to pause_execution's behavior.
        execution_module['executions_repo'].query_by_pipeline_execution.assert_not_called()
