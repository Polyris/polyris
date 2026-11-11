"""Acceptance test for task 1.20 — wait_for AND semantics.

Exercises the full end-to-end flow from check_assets (gate + subscription writes)
through notify_asset_subscribers (AND-gate check + signal) using real DDB tables
via moto. No mocked DAL repos.

Scenarios (PLAN §2.4):
  Sc1 — wait_for=[a, b]: a arrives first (gate incomplete), b arrives → signal
  Sc2 — wait_for=[a, b]: b arrives first, a arrives → signal
  Sc3 — a present at registration (gate = {b}), b arrives later → signal on b
  Sc5 — wait_for=[a]: single asset, signal on a
  Sc6 — wait_for=[a | b] + task dep pending: a arrives → subscription deleted, no signal sent
  Sc7 — legacy row with only assets_ready=True: notify signals immediately

Each test loads check_assets and notify_asset_subscribers in isolation using the
same sys.modules['dal'] shadowing technique as test_wait_for_or_sibling_cleanup.py,
so the two Lambdas' dal packages don't collide.
"""

import importlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

CHECK_DIR = Path(__file__).parent.parent.parent / "sam/lambdas/check_assets"
NOTIFY_DIR = Path(__file__).parent.parent.parent / "sam/lambdas/notify_asset_subscribers"

TOKENS_TABLE = "acc-tokens"
SUBS_TABLE = "acc-subs"
EVENTS_TABLE = "acc-events"


def _load_check_assets():
    """Load check_assets index + dal in isolation."""
    spec_dal = importlib.util.spec_from_file_location("_check_dal_acc", CHECK_DIR / "dal/__init__.py")
    dal = importlib.util.module_from_spec(spec_dal)
    spec_dal.loader.exec_module(dal)

    old_dal = sys.modules.get("dal")
    sys.modules["dal"] = dal
    try:
        spec = importlib.util.spec_from_file_location("_check_index_acc", CHECK_DIR / "index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old_dal is None:
            sys.modules.pop("dal", None)
        else:
            sys.modules["dal"] = old_dal
    return mod, dal


def _load_notify():
    """Load notify_asset_subscribers index + dal in isolation."""
    spec_dal = importlib.util.spec_from_file_location("_notify_dal_acc", NOTIFY_DIR / "dal/__init__.py")
    dal = importlib.util.module_from_spec(spec_dal)
    spec_dal.loader.exec_module(dal)

    old_dal = sys.modules.get("dal")
    sys.modules["dal"] = dal
    try:
        spec = importlib.util.spec_from_file_location("_notify_index_acc", NOTIFY_DIR / "index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old_dal is None:
            sys.modules.pop("dal", None)
        else:
            sys.modules["dal"] = old_dal
    return mod, dal


@pytest.fixture
def modules():
    check_mod, check_dal = _load_check_assets()
    notify_mod, notify_dal = _load_notify()
    return check_mod, check_dal, notify_mod, notify_dal


def _create_tables(dynamodb):
    tokens_tbl = dynamodb.create_table(
        TableName=TOKENS_TABLE,
        KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
        AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
        BillingMode='PAY_PER_REQUEST',
    )
    subs_tbl = dynamodb.create_table(
        TableName=SUBS_TABLE,
        KeySchema=[
            {'AttributeName': 'dependency_key', 'KeyType': 'HASH'},
            {'AttributeName': 'subscriber', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'dependency_key', 'AttributeType': 'S'},
            {'AttributeName': 'subscriber', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    events_tbl = dynamodb.create_table(
        TableName=EVENTS_TABLE,
        KeySchema=[
            {'AttributeName': 'asset_name', 'KeyType': 'HASH'},
            {'AttributeName': 'event_time', 'KeyType': 'RANGE'},
        ],
        AttributeDefinitions=[
            {'AttributeName': 'asset_name', 'AttributeType': 'S'},
            {'AttributeName': 'event_time', 'AttributeType': 'S'},
        ],
        BillingMode='PAY_PER_REQUEST',
    )
    return tokens_tbl, subs_tbl, events_tbl


def _base_token(exec_name, wait_for_meta, *, wait_token='tok-1'):
    return {
        'execution_name': exec_name,
        'wait_for': json.dumps(wait_for_meta),
        'dependencies': '[]',
        'trigger_rule': 'all_success',
        'date': '2026-01-01',
        'pipeline_execution_short': 'run-2026-01-01-abc1',
        'pipeline_execution': 'my-pipeline-run-2026-01-01-abc12345',
        'pipeline_name': 'my-pipeline',
        'wait_token': wait_token,
    }


def _check_input(wait_for_sfn, exec_name, task_name='consumer', wait_token='tok-1'):
    return {
        'wait_for': wait_for_sfn,
        'task_name': task_name,
        'wait_token': wait_token,
        'execution_name': exec_name,
        'ttl': 9999999999,
        'current_date': '2026-01-01',
    }


def _notify_input(asset_name, uri='s3://bucket/x', event_time='2026-01-01T09:00:00Z'):
    return {
        'outlets': [{'name': asset_name, 'uri': uri}],
        'event_time': event_time,
    }


class TestWaitForAndSemantics:

    def _setup_repos(self, monkeypatch, check_mod, check_dal, notify_mod, notify_dal):
        """Return (c_repos, n_repos) dicts after patching each Lambda's module-level repos."""
        c_repos = {
            'tokens': check_dal.TokensRepo(TOKENS_TABLE),
            'subs': check_dal.SubscriptionsRepo(SUBS_TABLE),
            'events': check_dal.AssetEventsRepo(EVENTS_TABLE),
        }
        n_repos = {
            'tokens': notify_dal.TokensRepo(TOKENS_TABLE),
            'subs': notify_dal.SubscriptionsRepo(SUBS_TABLE),
            'events': notify_dal.AssetEventsRepo(EVENTS_TABLE),
        }
        monkeypatch.setattr(check_mod, 'tokens_repo', c_repos['tokens'])
        monkeypatch.setattr(check_mod, 'subscriptions_repo', c_repos['subs'])
        monkeypatch.setattr(check_mod, 'asset_events_repo', c_repos['events'])
        monkeypatch.setattr(notify_mod, 'tokens_repo', n_repos['tokens'])
        monkeypatch.setattr(notify_mod, 'subscriptions_repo', n_repos['subs'])
        monkeypatch.setattr(notify_mod, 'asset_events_repo', n_repos['events'])
        return c_repos, n_repos

    def test_a_then_b(self, monkeypatch, mocker, modules):
        """Sc1 — wait_for=[a, b]: a arrives first → gate incomplete; b → signal sent."""
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and001'
        WAIT_FOR_META = [{'name': 'asset_a'}, {'name': 'asset_b'}]
        WAIT_FOR_SFN = [{'asset_name': 'asset_a'}, {'asset_name': 'asset_b'}]

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, _ = _create_tables(dynamodb)

            tokens_tbl.put_item(Item=_base_token(EXEC, WAIT_FOR_META))

            c_repos, _ = self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            mocker.patch.object(notify_mod, 'EVALUATE_DEPS_LAMBDA', '')
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            # Registration: neither asset ready → gate={a,b}, subs for a and b saved
            check_mod.handler(_check_input(WAIT_FOR_SFN, EXEC), None)

            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item')
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item')
            gate_before = tokens_tbl.get_item(Key={'execution_name': EXEC})['Item'].get('wait_for_assets')
            assert set(gate_before) == {'asset_a', 'asset_b'}

            # Event a arrives → gate not complete, no signal, sub for a deleted
            notify_mod.handler(_notify_input('asset_a'), None)
            mock_signal.assert_not_called()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item')

            # Event b arrives → gate complete, signal sent, sub for b deleted
            notify_mod.handler(_notify_input('asset_b'), None)
            mock_signal.assert_called_once()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item') is None

    def test_b_then_a(self, monkeypatch, mocker, modules):
        """Sc2 — wait_for=[a, b]: b arrives first → gate incomplete; a → signal sent."""
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and002'
        WAIT_FOR_META = [{'name': 'asset_a'}, {'name': 'asset_b'}]
        WAIT_FOR_SFN = [{'asset_name': 'asset_a'}, {'asset_name': 'asset_b'}]

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, _ = _create_tables(dynamodb)
            tokens_tbl.put_item(Item=_base_token(EXEC, WAIT_FOR_META))

            self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            mocker.patch.object(notify_mod, 'EVALUATE_DEPS_LAMBDA', '')
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            check_mod.handler(_check_input(WAIT_FOR_SFN, EXEC), None)

            # b first
            notify_mod.handler(_notify_input('asset_b'), None)
            mock_signal.assert_not_called()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item') is None
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item')

            # a completes the gate
            notify_mod.handler(_notify_input('asset_a'), None)
            mock_signal.assert_called_once()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None

    def test_a_ready_at_registration_b_later(self, monkeypatch, mocker, modules):
        """Sc3 — a present at registration (gate = {b}); b arrives → signal sent."""
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and003'
        WAIT_FOR_META = [{'name': 'asset_a'}, {'name': 'asset_b'}]
        WAIT_FOR_SFN = [{'asset_name': 'asset_a'}, {'asset_name': 'asset_b'}]

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, events_tbl = _create_tables(dynamodb)
            tokens_tbl.put_item(Item=_base_token(EXEC, WAIT_FOR_META))

            # a is already present in the events table at registration time
            events_tbl.put_item(Item={
                'asset_name': 'asset_a',
                'event_time': '2026-01-01T06:00:00Z',
                'uri': 's3://bucket/a',
            })

            self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            mocker.patch.object(notify_mod, 'EVALUATE_DEPS_LAMBDA', '')
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            # Registration: a is ready → gate = {b} only; subscription only for b
            check_mod.handler(_check_input(WAIT_FOR_SFN, EXEC), None)

            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item')
            gate = tokens_tbl.get_item(Key={'execution_name': EXEC})['Item'].get('wait_for_assets')
            assert set(gate) == {'asset_b'}

            # b arrives → gate {b} ⊆ {b} → signal
            notify_mod.handler(_notify_input('asset_b'), None)
            mock_signal.assert_called_once()

    def test_single_asset(self, monkeypatch, mocker, modules):
        """Sc5 — wait_for=[a]: a arrives → signal sent immediately."""
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and005'
        WAIT_FOR_META = [{'name': 'asset_a'}]
        WAIT_FOR_SFN = [{'asset_name': 'asset_a'}]

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, _ = _create_tables(dynamodb)
            tokens_tbl.put_item(Item=_base_token(EXEC, WAIT_FOR_META))

            self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            mocker.patch.object(notify_mod, 'EVALUATE_DEPS_LAMBDA', '')
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            check_mod.handler(_check_input(WAIT_FOR_SFN, EXEC), None)

            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item')
            gate = tokens_tbl.get_item(Key={'execution_name': EXEC})['Item'].get('wait_for_assets')
            assert set(gate) == {'asset_a'}

            notify_mod.handler(_notify_input('asset_a'), None)
            mock_signal.assert_called_once()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None

    def test_or_group_case_b_credits_asset(self, monkeypatch, mocker, modules):
        """Sc6 — wait_for=[a | b] + task dep pending: a arrives → credited (sub deleted), no signal.

        EVALUATE_DEPS_LAMBDA is set and returns is_ready=False (task deps still pending).
        The asset arrival is recorded in assets_ready_set so when task deps eventually
        complete, notify_dependents sees assets_ready=True and signals the task.
        """
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and006'
        WAIT_FOR_META = [{'operator': 'OR', 'assets': [{'name': 'asset_a'}, {'name': 'asset_b'}]}]
        WAIT_FOR_SFN = [{'operator': 'OR', 'assets': [{'asset_name': 'asset_a'}, {'asset_name': 'asset_b'}]}]

        fake_lambda = mocker.MagicMock()
        fake_lambda.invoke.return_value = {
            'Payload': io.BytesIO(json.dumps({'is_ready': False}).encode()),
        }

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, _ = _create_tables(dynamodb)
            tokens_tbl.put_item(Item=_base_token(EXEC, WAIT_FOR_META))

            self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            # Case B: eval_deps returns not-ready (task deps still pending)
            monkeypatch.setattr(notify_mod, 'EVALUATE_DEPS_LAMBDA', 'fake-eval-arn')
            mocker.patch.object(notify_mod, '_get_lambda', return_value=fake_lambda)
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            # OR spec → no gate written; subscriptions for both a and b
            check_mod.handler(_check_input(WAIT_FOR_SFN, EXEC, task_name='consumer'), None)
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item')
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}).get('Item')
            # Confirm no gate for OR spec
            assert 'wait_for_assets' not in tokens_tbl.get_item(Key={'execution_name': EXEC})['Item']

            # a arrives; eval_deps says not_ready → subscription deleted (asset credited), no signal
            notify_mod.handler(_notify_input('asset_a'), None)
            mock_signal.assert_not_called()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None

            # assets_ready_set now contains 'asset_a', assets_ready = True
            record = tokens_tbl.get_item(Key={'execution_name': EXEC})['Item']
            assert 'asset_a' in record.get('assets_ready_set', set())
            assert record.get('assets_ready') is True

    def test_legacy_assets_ready_bool(self, monkeypatch, mocker, modules):
        """Sc7 — legacy row (only assets_ready=True, no sets or gate): notify signals immediately.

        Old rows written before the AND-gate fields existed only have the bool flag.
        notify_asset_subscribers must signal these without getting stuck — no gate is
        present, so the absence of wait_for_assets is treated as 'no gate required'.
        """
        from moto import mock_aws
        import boto3

        check_mod, check_dal, notify_mod, notify_dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(check_dal, '_dynamodb', None)
        monkeypatch.setattr(notify_dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-and007'

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')
            tokens_tbl, subs_tbl, _ = _create_tables(dynamodb)

            # Legacy token row: only the bool, no gate or set fields
            tokens_tbl.put_item(Item={
                'execution_name': EXEC,
                'wait_for': json.dumps([{'name': 'asset_a'}]),
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'date': '2026-01-01',
                'pipeline_execution_short': 'run-2026-01-01-leg1',
                'pipeline_execution': 'my-pipeline-run-2026-01-01-leg12345',
                'pipeline_name': 'my-pipeline',
                'wait_token': 'tok-legacy',
                'assets_ready': True,  # only the legacy bool
            })
            # Manual subscription row (simulating what check_assets would have written
            # on a previous code version that didn't write wait_for_assets)
            subs_tbl.put_item(Item={
                'dependency_key': 'asset:asset_a',
                'subscriber': 'consumer',
                'wait_token': 'tok-legacy',
                'execution_name': EXEC,
                'subscription_type': 'asset',
            })

            self._setup_repos(monkeypatch, check_mod, check_dal, notify_mod, notify_dal)
            mocker.patch.object(notify_mod, 'EVALUATE_DEPS_LAMBDA', '')
            mock_signal = mocker.patch.object(notify_mod, '_send_task_success', return_value='sent')

            # Legacy row has no gate → no AND check; signal sent immediately
            notify_mod.handler(_notify_input('asset_a'), None)
            mock_signal.assert_called_once()
            assert subs_tbl.get_item(Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}).get('Item') is None
