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
        res = WebhookNotifier({'url_param': '/polyris/alerts/myorg/dev/acme/hook'}).notify(failure)
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


# ── the platform's rows, as notify reads them (moto) ──────────────────────

REGION = 'us-east-1'
EXECUTION = 'extract-2026-06-23-abc'


class Platform:
    """The registry and the task rows that the platform wrote, in moto."""

    def __init__(self, ddb):
        self.ddb = ddb

    def configure(self, pipeline, **channels):
        self.ddb.Table('registry').put_item(Item={'pipeline_name': pipeline, 'alert_config': {
            'enabled_channels': list(channels), **channels}})

    def task_row(self, execution_name=EXECUTION, *, pipeline='acme-daily', task='extract', date='2026-06-23'):
        self.ddb.Table('tokens').put_item(Item={'execution_name': execution_name, 'pipeline_name': pipeline,
                                                'task_name': task, 'date': date, 'status': 'waiting_decision'})


@pytest.fixture
def platform(monkeypatch):
    import boto3
    import registry
    from moto import mock_aws

    for name, value in {'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_REGION': REGION,
                        'AWS_DEFAULT_REGION': REGION, 'PIPELINES_TABLE': 'registry', 'TOKENS_TABLE': 'tokens'}.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        monkeypatch.setattr(registry, '_dynamodb', None)
        monkeypatch.setattr(registry, '_tokens', None)
        ddb = boto3.resource('dynamodb', region_name=REGION)
        for table, key in (('registry', 'pipeline_name'), ('tokens', 'execution_name')):
            ddb.create_table(TableName=table, KeySchema=[{'AttributeName': key, 'KeyType': 'HASH'}],
                             AttributeDefinitions=[{'AttributeName': key, 'AttributeType': 'S'}],
                             BillingMode='PAY_PER_REQUEST')
        yield Platform(ddb)


@pytest.fixture
def post(mocker):
    return mocker.patch('notifiers._http_post_json', return_value=200)


# ── action dispatch framework (paid actions absent in OSS) ────────────────

class TestActionFramework:
    def test_unknown_action_no_ops(self, platform):
        import index
        platform.task_row()
        # No build registers this action; the paid one registers interactive_slack.
        out = index.handler({'action': 'no_such_action', 'pipeline_name': 'acme-daily',
                             'failure': {'execution_name': EXECUTION}}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'unknown_action'

    def test_register_action_adds_one(self):
        from actions import register_action, dispatch_action
        register_action('dummy_action', lambda e: {'action': 'dummy_action', 'delivered': True})
        assert dispatch_action('dummy_action', {})['delivered'] is True


# ── handler dispatch ──────────────────────────────────────────────────────

class TestHandler:
    """Single shape: one of the pipeline's enabled channels, its config from the registry, never the event."""

    def test_dispatches_to_channel_with_the_registrys_config(self, mocker, platform, post, failure):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/p'})
        platform.task_row()
        secret = mocker.patch('notifiers._get_secret', return_value='https://hooks.example.com/x')
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'failure': failure}, None)
        assert out['channel'] == 'webhook'
        assert out['delivered'] is True
        secret.assert_called_once_with('/p')
        assert post.call_args.args[1]['task'] == 'extract'

    def test_a_config_in_the_event_is_ignored(self, mocker, platform, failure):
        """A pipeline's roles may invoke the function: an event must not point an alert anywhere (ADR #142 §5)."""
        import index
        platform.configure('acme-daily', webhook={'url_param': '/polyris/alerts/acme/dev/p/webhook-url'})
        platform.task_row()
        secret = mocker.patch('notifiers._get_secret', return_value=None)
        index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'config': {'url_param': '/elsewhere'},
                       'failure': failure}, None)
        secret.assert_called_once_with('/polyris/alerts/acme/dev/p/webhook-url')

    def test_a_channel_the_pipeline_has_not_enabled_is_not_delivered(self, platform, post, failure):
        import index
        platform.configure('acme-daily')
        platform.task_row()
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'config': {'url_param': '/p'},
                             'failure': failure}, None)
        assert (out['delivered'], out['reason']) == (False, 'not_enabled')
        assert not post.called

    def test_no_channel_is_safe(self):
        import index
        out = index.handler({'channel': '', 'failure': {}}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'no_channel'

    def test_unknown_channel_is_safe(self, platform, failure):
        import index
        platform.configure('acme-daily', telegram={})
        platform.task_row()
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'telegram', 'failure': failure}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'unknown_channel'

    def test_channel_error_does_not_raise(self, mocker, platform, failure):
        import index
        platform.configure('acme-daily', webhook={})
        platform.task_row()
        mocker.patch('index.get_notifier', side_effect=RuntimeError('boom'))
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'failure': failure}, None)
        assert out['delivered'] is False
        assert out['reason'] == 'delivery_error'


class TestBatchMode:
    """Batch shape: {pipeline_name, failure} → Lambda reads config + loops channels."""

    def test_reads_config_and_fans_out(self, mocker, platform, failure):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/s'}, webhook2={'url_param': '/p'})
        platform.task_row()
        deliver = mocker.patch('index._deliver_one', return_value={'delivered': True, 'status': 200})
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert len(out['channels']) == 2
        assert {c.args[0] for c in deliver.call_args_list} == {'webhook', 'webhook2'}

    def test_no_channels_is_clean_noop(self, mocker, platform, failure):
        import index
        platform.configure('acme-daily')
        platform.task_row()
        deliver = mocker.patch('index._deliver_one')
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert out['channels'] == []
        deliver.assert_not_called()

    def test_one_channel_failing_does_not_block_others(self, mocker, platform, failure):
        import index
        platform.configure('acme-daily', webhook={}, webhook2={})
        platform.task_row()
        mocker.patch('index._deliver_one', side_effect=[
            {'channel': 'webhook', 'delivered': False, 'reason': 'http_error'},
            {'channel': 'webhook2', 'delivered': True, 'status': 202},
        ])
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert out['delivered'] is True
        assert len(out['channels']) == 2

    def test_a_pipeline_without_config_is_a_clean_noop(self, platform, post, failure):
        import index
        platform.task_row()
        out = index.handler({'pipeline_name': 'acme-daily', 'failure': failure}, None)
        assert (out['delivered'], out['channels']) == (True, [])
        assert not post.called


class TestTheFailureComesFromThePlatform:
    """A pipeline's roles may invoke notify (ADR #142 §5): a forged event must not reach a webhook, a Slack
    button or a PagerDuty incident with a link, an execution or a task that is not the platform's."""

    def test_a_forged_failure_keeps_only_its_error_and_the_rest_comes_from_the_row(self, mocker, platform, post):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/p'})
        platform.task_row()
        mocker.patch('notifiers._get_secret', return_value='https://hooks.example.com/x')
        index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'failure': {
            'execution_name': EXECUTION, 'error': 'boom', 'task_name': 'payroll', 'date': '1999-01-01',
            'pipeline_name': 'billing', 'links': [{'href': 'https://evil.example/login', 'text': 'Sign in'}]}}, None)
        payload = post.call_args.args[1]
        assert (payload['pipeline'], payload['task'], payload['date'], payload['error']) == (
            'acme-daily', 'extract', '2026-06-23', 'boom')
        assert 'evil.example' not in str(payload) and 'links' not in payload

    @pytest.mark.parametrize("failure, reason", [
        ({'execution_name': 'load-2026-06-23-xyz'}, 'unknown_execution'),     # a row of another pipeline
        ({'execution_name': 'gone-2026-06-23-xyz'}, 'unknown_execution'),     # no row
        ({'execution_name': '_run_acme-daily-run-2026-06-23-00000000'}, 'no_execution'),  # an internal row
        ({}, 'no_execution'),
    ])
    def test_an_execution_that_is_no_task_row_of_the_pipeline_sends_nothing(self, platform, post, failure, reason):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/p'})
        platform.task_row('load-2026-06-23-xyz', pipeline='billing', task='load')
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'failure': failure}, None)
        assert (out['delivered'], out['reason']) == (False, reason)
        assert not post.called

    def test_an_action_for_another_pipelines_execution_is_refused_before_it_runs(self, mocker, platform):
        import index
        platform.task_row('load-2026-06-23-xyz', pipeline='billing', task='load')
        action = mocker.patch('index.dispatch_action')
        out = index.handler({'action': 'interactive_slack', 'pipeline_name': 'acme-daily',
                             'failure': {'execution_name': 'load-2026-06-23-xyz'}}, None)
        assert (out['action'], out['delivered'], out['reason']) == ('interactive_slack', False, 'unknown_execution')
        assert not action.called

    def test_an_action_gets_the_failure_the_row_gives(self, mocker, platform):
        import index
        platform.task_row()
        action = mocker.patch('index.dispatch_action', return_value={'delivered': True})
        index.handler({'action': 'live_pagerduty', 'pipeline_name': 'acme-daily', 'failure': {
            'execution_name': EXECUTION, 'error': 'TaskFailed', 'task_name': 'x', 'links': ['y']}}, None)
        assert action.call_args.args[1]['failure'] == {
            'pipeline_name': 'acme-daily', 'task_name': 'extract', 'execution_name': EXECUTION,
            'date': '2026-06-23', 'error': 'TaskFailed'}

    def test_a_row_that_cannot_be_read_sends_nothing_and_says_why(self, mocker, platform, post, monkeypatch):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/p'})
        monkeypatch.setenv('TOKENS_TABLE', 'no-such-table')
        error = mocker.patch('index.log.error')
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook',
                             'failure': {'execution_name': EXECUTION}}, None)
        assert (out['delivered'], out['reason']) == (False, 'row_unreadable')
        assert 'ResourceNotFoundException' in str(error.call_args) and not post.called


class TestTestAlert:
    """A test goes through a channel the pipeline saved and enabled, with a fixed text and no execution."""

    def test_a_saved_enabled_channel_gets_the_test_message(self, mocker, platform, post):
        import index
        platform.configure('acme-daily', webhook={'url_param': '/p'})
        mocker.patch('notifiers._get_secret', return_value='https://hooks.example.com/x')
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'test': True}, None)
        assert out['delivered'] is True
        assert post.call_args.args[1]['error'] == index.TEST_ERROR

    def test_a_channel_that_is_not_enabled_gets_no_test(self, platform, post):
        import index
        platform.configure('acme-daily')
        out = index.handler({'pipeline_name': 'acme-daily', 'channel': 'webhook', 'test': True}, None)
        assert (out['delivered'], out['reason']) == (False, 'not_enabled')
        assert not post.called

    def test_an_action_is_never_a_test(self, mocker, platform):
        import index
        action = mocker.patch('index.dispatch_action')
        out = index.handler({'action': 'interactive_slack', 'pipeline_name': 'acme-daily', 'test': True}, None)
        assert (out['delivered'], out['reason']) == (False, 'not_a_test_action')
        assert not action.called


class TestOverlayImportGuard:
    """notifiers.py's optional `ee` import (Principle #44): only the overlay's own absence is the free build."""

    @staticmethod
    def _load(monkeypatch, tmp_path, init=None):
        import importlib.util
        import sys
        from pathlib import Path

        for name in [m for m in sys.modules if m == "ee" or m.startswith("ee.")]:
            monkeypatch.delitem(sys.modules, name)
        if init is None:
            monkeypatch.setitem(sys.modules, "ee", None)        # `import ee` -> ModuleNotFoundError(name='ee')
        else:
            (tmp_path / "ee").mkdir()
            (tmp_path / "ee" / "__init__.py").write_text(init)
            monkeypatch.syspath_prepend(str(tmp_path))
        spec = importlib.util.spec_from_file_location("notifiers_guard_probe",
                                                      Path(__file__).resolve().parent / "notifiers.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_without_the_overlay_the_free_channels_load(self, monkeypatch, tmp_path):
        assert "webhook" in self._load(monkeypatch, tmp_path).NOTIFIERS

    def test_a_broken_overlay_fails_loudly(self, monkeypatch, tmp_path):
        with pytest.raises(ModuleNotFoundError, match="slack_sdk"):
            self._load(monkeypatch, tmp_path, init="import slack_sdk_missing_for_the_test as slack_sdk\n")
