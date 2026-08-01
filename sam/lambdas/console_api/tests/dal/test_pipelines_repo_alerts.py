"""
Unit tests for dal.pipelines_repo alert-config methods (ADR #103).

Pattern follows test_api_tokens_repo.py — mock the .table property to short-
circuit the dynamodb.Table(...) lookup. No moto, no AWS calls.
"""


def _repo(mocker, table_mock):
    from dal.pipelines_repo import PipelinesRepo
    repo = PipelinesRepo()
    mocker.patch.object(PipelinesRepo, 'table',
                        new_callable=mocker.PropertyMock,
                        return_value=table_mock)
    return repo


class TestGetAlertConfig:
    def test_returns_config_when_present(self, mocker):
        cfg = {
            'enabled_channels': ['slack', 'pagerduty'],
            'slack': {'channel': '#acme-alerts', 'mentions': ['@oncall'],
                      'webhook_param': '/polyris/alerts/p/slack-webhook'},
            'pagerduty': {'severity': 'critical',
                          'routing_key_param': '/polyris/alerts/p/pd-key'},
        }
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': {'pipeline_name': 'p', 'alert_config': cfg}}
        repo = _repo(mocker, table)
        got = repo.get_alert_config('p')
        assert got['enabled_channels'] == ['slack', 'pagerduty']
        assert got['slack']['channel'] == '#acme-alerts'
        # secrets stored as SSM parameter names, never plaintext
        assert got['slack']['webhook_param'].startswith('/polyris/')
        assert got['pagerduty']['routing_key_param'].startswith('/polyris/')
        assert 'routing_key' not in got['pagerduty']  # no plaintext key

    def test_missing_pipeline_returns_empty_safe(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {}
        repo = _repo(mocker, table)
        got = repo.get_alert_config('nope')
        assert got == {'enabled_channels': []}

    def test_pipeline_without_alert_config_returns_empty_safe(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': {'pipeline_name': 'p'}}
        repo = _repo(mocker, table)
        got = repo.get_alert_config('p')
        assert got == {'enabled_channels': []}

    def test_non_dict_alert_config_returns_empty_safe(self, mocker):
        # defensive: a corrupt/legacy string value must not crash alerting
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': {'pipeline_name': 'p', 'alert_config': 'oops'}}
        repo = _repo(mocker, table)
        assert repo.get_alert_config('p') == {'enabled_channels': []}

    def test_enabled_channels_defaulted_when_absent(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {'pipeline_name': 'p', 'alert_config': {'slack': {'channel': '#c'}}}
        }
        repo = _repo(mocker, table)
        got = repo.get_alert_config('p')
        assert got['enabled_channels'] == []
        assert got['slack']['channel'] == '#c'


class TestSetAlertConfig:
    def test_writes_only_alert_config_attr(self, mocker):
        table = mocker.MagicMock()
        table.update_item.return_value = {}
        repo = _repo(mocker, table)
        cfg = {'enabled_channels': ['slack'], 'slack': {'channel': '#c', 'mentions': []}}
        repo.set_alert_config('p', cfg)
        kwargs = table.update_item.call_args.kwargs
        assert kwargs['Key'] == {'pipeline_name': 'p'}
        assert 'alert_config' in kwargs['UpdateExpression']
        assert kwargs['ExpressionAttributeValues'][':cfg'] == cfg


class TestMigrateSlackChannel:
    def test_seeds_from_legacy_channel(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {'pipeline_name': 'p', 'slack_channel': '#legacy'}
        }
        table.update_item.return_value = {}
        repo = _repo(mocker, table)
        cfg = repo.migrate_slack_channel('p')
        assert cfg == {
            'enabled_channels': ['slack'],
            'slack': {'channel': '#legacy', 'mentions': []},
        }
        # actually persisted
        assert table.update_item.called

    def test_idempotent_when_alert_config_exists(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {
            'Item': {'pipeline_name': 'p', 'slack_channel': '#legacy',
                     'alert_config': {'enabled_channels': ['slack']}}
        }
        repo = _repo(mocker, table)
        assert repo.migrate_slack_channel('p') is None
        assert not table.update_item.called

    def test_no_legacy_channel_nothing_to_migrate(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': {'pipeline_name': 'p'}}
        repo = _repo(mocker, table)
        assert repo.migrate_slack_channel('p') is None
        assert not table.update_item.called

    def test_missing_pipeline_nothing_to_migrate(self, mocker):
        table = mocker.MagicMock()
        table.get_item.return_value = {}
        repo = _repo(mocker, table)
        assert repo.migrate_slack_channel('nope') is None



class TestGlobalSettings:
    """Global decision-timeout settings (ADR #103 1b) — one shared registry record."""

    def _repo(self, mocker, item=None):
        from dal.pipelines_repo import PipelinesRepo
        repo = PipelinesRepo()
        table = mocker.MagicMock()
        table.get_item.return_value = {'Item': item} if item else {}
        mocker.patch.object(PipelinesRepo, 'table', property(lambda self: table))
        return repo, table

    def test_defaults_when_missing(self, mocker):
        repo, _ = self._repo(mocker, item=None)
        assert repo.get_global_settings() == {'decision_timeout_seconds': 18000}

    def test_reads_stored_value(self, mocker):
        repo, _ = self._repo(mocker, item={
            'pipeline_name': '__global_settings__',
            'decision_timeout_seconds': 3600,
        })
        assert repo.get_global_settings()['decision_timeout_seconds'] == 3600

    def test_bad_value_degrades_to_default(self, mocker):
        repo, _ = self._repo(mocker, item={
            'pipeline_name': '__global_settings__',
            'decision_timeout_seconds': 'not-a-number',
        })
        assert repo.get_global_settings()['decision_timeout_seconds'] == 18000

    def test_set_writes_to_global_record(self, mocker):
        repo, table = self._repo(mocker, item=None)
        out = repo.set_decision_timeout(7200)
        assert out == {'decision_timeout_seconds': 7200}
        table.update_item.assert_called_once()
        kwargs = table.update_item.call_args[1]
        assert kwargs['Key'] == {'pipeline_name': '__global_settings__'}
        assert kwargs['ExpressionAttributeValues'][':t'] == 7200
