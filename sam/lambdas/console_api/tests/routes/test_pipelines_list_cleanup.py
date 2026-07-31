"""Unit tests for routes.pipelines_list.list_pipelines.

Added in v0.77.1 cleanup. The function had an unused `sla_start = (now
- timedelta(days=SLA_DAYS))` computation that did nothing. These tests
verify the function still works correctly without that calculation.

Scope is intentionally minimal — just enough to lock the happy path
behavior. The function is large (200+ lines including stats branch)
but the cleaned-up line was at the entry, before any branches.

Conventions per ADR #26: pytest-mock `mocker` fixture.
"""
import json

import pytest


@pytest.fixture
def pipelines_module(mocker):
    """Patch I/O dependencies for list_pipelines."""
    from routes import pipelines_list as pipelines_module

    pipelines_repo = mocker.MagicMock()
    pipelines_repo.list_all.return_value = []
    mocker.patch.object(pipelines_module, 'pipelines_repo', pipelines_repo)

    executions_repo = mocker.MagicMock()
    executions_repo.query_by_date.return_value = []
    executions_repo.scan.return_value = []
    mocker.patch.object(pipelines_module, 'executions_repo', executions_repo)

    return {
        'pipelines_repo': pipelines_repo,
        'executions_repo': executions_repo,
    }


def _body(resp):
    return json.loads(resp['body'])


class TestListPipelines:
    def test_empty_registry_returns_empty_list(self, pipelines_module):
        from routes.pipelines_list import list_pipelines
        resp = list_pipelines({'queryStringParameters': {}})
        assert resp['statusCode'] == 200
        body = _body(resp)
        assert body['pipelines'] == []

    def test_returns_registry_entries(self, pipelines_module):
        """v0.77.1 cleanup didn't touch the registry → response flow.
        Sanity check that pipelines still surface."""
        from routes.pipelines_list import list_pipelines
        pipelines_module['pipelines_repo'].list_all.return_value = [
            {
                'pipeline_name': 'acme-daily',
                'sfn_arn': 'arn:...:acme-daily',
                'description': 'Daily ETL',
                'pipeline_group': 'acme',
                'schedule': 'cron(0 3 * * ? *)',
            }
        ]
        resp = list_pipelines({'queryStringParameters': {}})
        body = _body(resp)
        assert len(body['pipelines']) == 1
        p = body['pipelines'][0]
        assert p['name'] == 'acme-daily'
        assert p['group'] == 'acme'
        assert p['paused'] is False  # default
        assert p['status'] == 'idle'

    def test_stats_param_false_skips_expensive_scan(self, pipelines_module):
        """Without ?stats=true, we shouldn't query executions at all —
        cheap path. v0.77.1 cleanup removed `sla_start` (only used by
        stats), so the no-stats path should still work."""
        from routes.pipelines_list import list_pipelines
        pipelines_module['pipelines_repo'].list_all.return_value = [
            {'pipeline_name': 'p', 'sfn_arn': 'a'},
        ]
        resp = list_pipelines({'queryStringParameters': {}})
        assert resp['statusCode'] == 200
        # Without stats=true, no executions queries happen.
        pipelines_module['executions_repo'].query_by_date.assert_not_called()

    def test_genuinely_manual_pipeline_has_no_asset_schedule(self, pipelines_module):
        """A pipeline with neither schedule nor asset_schedule set (a real
        manual-only pipeline) must get asset_schedule=None — distinguishing
        it from an asset-triggered pipeline is the whole point of this field."""
        from routes.pipelines_list import list_pipelines
        pipelines_module['pipelines_repo'].list_all.return_value = [
            {'pipeline_name': 'manual-adhoc', 'sfn_arn': 'a'},
        ]
        resp = list_pipelines({'queryStringParameters': {}})
        p = _body(resp)['pipelines'][0]
        assert p['schedule'] == ''
        assert p['asset_schedule'] is None

    def test_asset_triggered_pipeline_surfaces_its_asset_schedule(self, pipelines_module):
        """An asset-triggered pipeline's registry item stores asset_schedule
        as a JSON string (written by Register_Pipeline) — it must come back
        parsed, not as a raw string, so the UI doesn't need to re-parse it."""
        from routes.pipelines_list import list_pipelines
        pipelines_module['pipelines_repo'].list_all.return_value = [
            {
                'pipeline_name': 'orders-analytics',
                'sfn_arn': 'arn:...:orders-analytics',
                'schedule': '',
                'asset_schedule': json.dumps({'operator': 'AND', 'assets': ['clean/orders']}),
            }
        ]
        resp = list_pipelines({'queryStringParameters': {}})
        p = _body(resp)['pipelines'][0]
        assert p['schedule'] == ''
        assert p['asset_schedule'] == {'operator': 'AND', 'assets': ['clean/orders']}

    def test_malformed_asset_schedule_degrades_to_none_not_a_crash(self, pipelines_module):
        """A corrupted/unparseable asset_schedule value must not take down
        the whole /api/pipelines response for every other pipeline."""
        from routes.pipelines_list import list_pipelines
        pipelines_module['pipelines_repo'].list_all.return_value = [
            {'pipeline_name': 'broken', 'sfn_arn': 'a', 'asset_schedule': 'not-json{{'},
        ]
        resp = list_pipelines({'queryStringParameters': {}})
        assert resp['statusCode'] == 200
        p = _body(resp)['pipelines'][0]
        assert p['asset_schedule'] is None
