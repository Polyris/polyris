"""Tests for the free settings route (GET decision-timeout) — ADR #103 1b."""

import json


class TestGetDecisionTimeout:
    def test_returns_stored_timeout(self, mocker):
        from routes.settings import get_decision_timeout
        mocker.patch('routes.settings.pipelines_repo.get_global_settings',
                     return_value={'decision_timeout_seconds': 3600})
        resp = get_decision_timeout({})
        assert resp['statusCode'] == 200
        assert json.loads(resp['body'])['decision_timeout_seconds'] == 3600

    def test_degrades_to_default_on_error(self, mocker):
        from routes.settings import get_decision_timeout
        mocker.patch('routes.settings.pipelines_repo.get_global_settings',
                     side_effect=Exception('ddb down'))
        resp = get_decision_timeout({})
        # Still 200 with the documented default — must not error the UI.
        assert resp['statusCode'] == 200
        assert json.loads(resp['body'])['decision_timeout_seconds'] == 18000

    def test_route_registered(self):
        from routes import settings
        from routing import Router
        r = Router()
        settings.register(r)
        assert ('GET', '/api/settings/decision-timeout') in r.table
