"""
Unit tests for the notify Lambda framework (ADR #103) — public (free) build.

pytest-mock throughout (ADR #26). All HTTP and SSM are mocked — no network, no
AWS. This is the OSS surface: the base framework + the webhook channel + batch
dispatch + the action-dispatch framework. Slack/PagerDuty channels and the paid
actions are in notify/ee/ and tested in the ee build; in the OSS build they are
absent and get_notifier / dispatch_action no-op on them.
"""

import pytest


@pytest.fixture
def failure():
    return {
        'pipeline_name': 'acme-daily',
        'task_name': 'extract',
        'execution_name': 'extract-2026-06-23-abc',
        'error': 'boom: connection refused',
        'date': '2026-06-23',
        'links': [{'href': 'https://console/exec', 'text': 'Task SFN Execution'}],
    }


@pytest.fixture(autouse=True)
def _no_real_ssm(mocker):
    fake = mocker.MagicMock()
    fake.get_parameter.return_value = {'Parameter': {'Value': 'https://secret-url'}}
    mocker.patch('notifiers._ssm_client', return_value=fake)
    return fake


# ── WebhookNotifier (the free channel) ────────────────────────────────────

class TestWebhookNotifier:
    def test_posts_failure_context(self, mocker, failure):
        from notifiers import WebhookNotifier
        post = mocker.patch('notifiers._http_post_json', return_value=200)
        res = WebhookNotifier({'url_param': '/polyris/alerts/acme/hook'}).notify(failure)
        assert res['delivered'] is True
        payload = post.call_args.args[1]
        assert payload['event'] == 'pipeline_task_failed'
        assert payload['pipeline'] == 'acme-daily'

    def test_missing_url_is_skip_not_crash(self, mocker, failure, _no_real_ssm):
        from notifiers import WebhookNotifier
        _no_real_ssm.get_parameter.side_effect = Exception('not found')
        res = WebhookNotifier({'url_param': '/missing'}).notify(failure)
        assert res['delivered'] is False
        assert res['reason'] == 'no_url'

    def test_http_error_is_captured(self, mocker, failure):
        from notifiers import WebhookNotifier
        mocker.patch('notifiers._http_post_json', side_effect=OSError('refused'))
        res = WebhookNotifier({'url_param': '/p'}).notify(failure)
        assert res['delivered'] is False
        assert res['reason'] == 'http_error'


# ── registry / get_notifier ───────────────────────────────────────────────

class TestRegistry:
    def test_webhook_resolves(self):
        from notifiers import get_notifier, WebhookNotifier
        assert isinstance(get_notifier('webhook', {}), WebhookNotifier)

    def test_unknown_channel_returns_none(self):
        from notifiers import get_notifier
        assert get_notifier('telegram', {}) is None

    def test_register_adds_a_channel(self):
        from notifiers import register, get_notifier, Notifier

        class DummyNotifier(Notifier):
            channel = 'dummy'

            def notify(self, failure):
                return {'channel': 'dummy', 'delivered': True}

        register('dummy', DummyNotifier)
        assert isinstance(get_notifier('dummy', {}), DummyNotifier)


# ── action dispatch framework (paid actions absent in OSS) ────────────────

class TestActionFramework:
    def test_unknown_action_no_ops(self):
        import index
        out = index.handler({'action': 'interactive_slack', 'pipeline_name': 'x'}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'unknown_action'

    def test_register_action_adds_one(self):
        from actions import register_action, dispatch_action
        register_action('dummy_action', lambda e: {'action': 'dummy_action', 'delivered': True})
        assert dispatch_action('dummy_action', {})['delivered'] is True


# ── handler dispatch ──────────────────────────────────────────────────────

class TestHandler:
    def test_dispatches_to_channel(self, mocker, failure):
        import index
        mocker.patch('notifiers._http_post_json', return_value=200)
        out = index.handler({'channel': 'webhook',
                             'config': {'url_param': '/p'},
                             'failure': failure}, None)
        assert out['channel'] == 'webhook'
        assert out['delivered'] is True

    def test_no_channel_is_safe(self):
        import index
        out = index.handler({'failure': {}}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'no_channel'

    def test_unknown_channel_is_safe(self):
        import index
        out = index.handler({'channel': 'telegram', 'config': {}, 'failure': {}}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'unknown_channel'

    def test_channel_error_does_not_raise(self, mocker, failure):
        import index
        mocker.patch('index.get_notifier', side_effect=RuntimeError('boom'))
        out = index.handler({'channel': 'webhook', 'config': {}, 'failure': failure}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'delivery_error'


class TestBatchMode:
    """Batch shape: {pipeline_name, failure} → Lambda reads config + loops channels."""

    def test_reads_config_and_fans_out(self, mocker, failure):
        import index
        mocker.patch('index.get_alert_config', return_value={
            'enabled_channels': ['webhook', 'webhook2'],
            'webhook': {'url_param': '/s'},
            'webhook2': {'url_param': '/p'},
        })
        deliver = mocker.patch('index._deliver_one',
                               return_value={'delivered': True, 'status': 200})
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert len(out['channels']) == 2
        calls = [c.args[0] for c in deliver.call_args_list]
        assert set(calls) == {'webhook', 'webhook2'}

    def test_no_channels_is_clean_noop(self, mocker, failure):
        import index
        mocker.patch('index.get_alert_config', return_value={'enabled_channels': []})
        deliver = mocker.patch('index._deliver_one')
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert out['channels'] == []
        deliver.assert_not_called()

    def test_one_channel_failing_does_not_block_others(self, mocker, failure):
        import index
        mocker.patch('index.get_alert_config', return_value={
            'enabled_channels': ['webhook', 'webhook2'],
            'webhook': {}, 'webhook2': {},
        })
        mocker.patch('index._deliver_one', side_effect=[
            {'channel': 'webhook', 'delivered': False, 'reason': 'http_error'},
            {'channel': 'webhook2', 'delivered': True, 'status': 202},
        ])
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert len(out['channels']) == 2

    def test_config_read_failure_degrades_to_noop(self, mocker, failure):
        import index
        mocker.patch('index.get_alert_config', return_value={})
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert out['channels'] == []
