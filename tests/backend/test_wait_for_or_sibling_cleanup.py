"""B3: OR-group sibling subscription cleanup after a successful wake-up signal.

After asset_a wakes a task with wait_for=[a | b], the subscription for
asset_b must be deleted (with execution_name ownership guard) so later
asset_b events don't trigger spurious evaluate_deps calls on a finished task.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

NOTIFY_DIR = Path(__file__).parent.parent.parent / "sam/lambdas/notify_asset_subscribers"


def _load_notify():
    """Load notify_asset_subscribers modules in isolation.

    Both modules share the name 'dal'/'index' across Lambdas.  We load the
    notify DAL under a private key, temporarily shadow sys.modules['dal'] so
    index.py's `from dal import ...` picks up the right module, then restore.
    """
    spec_dal = importlib.util.spec_from_file_location(
        "_notify_dal_isolated", NOTIFY_DIR / "dal/__init__.py"
    )
    dal = importlib.util.module_from_spec(spec_dal)
    spec_dal.loader.exec_module(dal)

    old_dal = sys.modules.get("dal")
    sys.modules["dal"] = dal
    try:
        spec_index = importlib.util.spec_from_file_location(
            "_notify_index_isolated", NOTIFY_DIR / "index.py",
        )
        index = importlib.util.module_from_spec(spec_index)
        spec_index.loader.exec_module(index)
    finally:
        if old_dal is None:
            sys.modules.pop("dal", None)
        else:
            sys.modules["dal"] = old_dal

    return index, dal


@pytest.fixture
def modules():
    return _load_notify()


class TestOrSiblingCleanup:

    def test_or_wakeup_deletes_sibling_subscriptions(self, monkeypatch, mocker, modules):
        """wait_for=[a | b]: asset_a wakes task → subscription for asset_b deleted."""
        from moto import mock_aws
        import boto3

        index, dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-or12345'
        TOKEN = 'fake-token-or'
        WAIT_FOR = json.dumps([{'operator': 'OR', 'assets': [{'name': 'asset_a'}, {'name': 'asset_b'}]}])

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-or',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            subs_tbl = dynamodb.create_table(
                TableName='dep-subscriptions-or',
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

            tokens_tbl.put_item(Item={
                'execution_name': EXEC,
                'wait_for': WAIT_FOR,
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'date': '2026-01-01',
                'pipeline_execution_short': 'run-2026-01-01-or123',
                'pipeline_execution': 'my-pipeline-run-2026-01-01-or123',
                'pipeline_name': 'my-pipeline',
                # No wait_for_assets — OR spec, no gate
            })

            for asset in ('asset_a', 'asset_b'):
                subs_tbl.put_item(Item={
                    'dependency_key': f'asset:{asset}',
                    'subscriber': 'consumer',
                    'wait_token': TOKEN,
                    'execution_name': EXEC,
                    'subscription_type': 'asset',
                })

            tok_repo = dal.TokensRepo('pipeline-tokens-or')
            subs_repo = dal.SubscriptionsRepo('dep-subscriptions-or')
            mocker.patch.object(index, 'tokens_repo', tok_repo)
            mocker.patch.object(index, 'subscriptions_repo', subs_repo)
            mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
            mocker.patch.object(index, '_send_task_success', return_value='sent')

            index.handler(
                {'outlets': [{'name': 'asset_a', 'uri': 's3://b/a'}], 'event_time': '2026-01-01T08:00:00Z'},
                None,
            )

            # asset_a subscription deleted by normal path
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}
            ).get('Item') is None

            # asset_b (sibling) deleted by B3 cleanup
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}
            ).get('Item') is None, "OR sibling subscription must be deleted after wake-up"

    def test_foreign_execution_name_sibling_untouched(self, monkeypatch, mocker, modules):
        """Sibling row that belongs to a different execution_name must not be deleted."""
        from moto import mock_aws
        import boto3

        index, dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal, '_dynamodb', None)

        MY_EXEC = 'consumer-2026-01-01-run1'
        OTHER_EXEC = 'consumer-2026-01-01-run2'
        TOKEN = 'fake-token-foreign'
        WAIT_FOR = json.dumps([{'operator': 'OR', 'assets': [{'name': 'asset_a'}, {'name': 'asset_b'}]}])

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-foreign',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            subs_tbl = dynamodb.create_table(
                TableName='dep-subscriptions-foreign',
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

            # Task record for the run that will be woken
            tokens_tbl.put_item(Item={
                'execution_name': MY_EXEC,
                'wait_for': WAIT_FOR,
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'date': '2026-01-01',
                'pipeline_execution_short': 'run-2026-01-01-run1',
                'pipeline_execution': 'my-pipeline-run-2026-01-01-run1',
                'pipeline_name': 'my-pipeline',
            })

            # asset_a → belongs to MY_EXEC (the one being woken)
            subs_tbl.put_item(Item={
                'dependency_key': 'asset:asset_a',
                'subscriber': 'consumer',
                'wait_token': TOKEN,
                'execution_name': MY_EXEC,
                'subscription_type': 'asset',
            })
            # asset_b → belongs to OTHER_EXEC (different parallel run)
            subs_tbl.put_item(Item={
                'dependency_key': 'asset:asset_b',
                'subscriber': 'consumer',
                'wait_token': 'other-token',
                'execution_name': OTHER_EXEC,
                'subscription_type': 'asset',
            })

            tok_repo = dal.TokensRepo('pipeline-tokens-foreign')
            subs_repo = dal.SubscriptionsRepo('dep-subscriptions-foreign')
            mocker.patch.object(index, 'tokens_repo', tok_repo)
            mocker.patch.object(index, 'subscriptions_repo', subs_repo)
            mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
            mocker.patch.object(index, '_send_task_success', return_value='sent')

            index.handler(
                {'outlets': [{'name': 'asset_a', 'uri': 's3://b/a'}], 'event_time': '2026-01-01T08:00:00Z'},
                None,
            )

            # asset_b row must survive — it belongs to OTHER_EXEC
            remaining = subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}
            ).get('Item')
            assert remaining is not None, "Sibling row for a different execution_name must not be deleted"
            assert remaining.get('execution_name') == OTHER_EXEC

    def test_failed_signal_does_not_cleanup_siblings(self, monkeypatch, mocker, modules):
        """When send_task_success returns 'failed', sibling subscriptions must not be touched."""
        from moto import mock_aws
        import boto3

        index, dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-fail1'
        WAIT_FOR = json.dumps([{'operator': 'OR', 'assets': [{'name': 'asset_a'}, {'name': 'asset_b'}]}])

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-fail',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            subs_tbl = dynamodb.create_table(
                TableName='dep-subscriptions-fail',
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

            tokens_tbl.put_item(Item={
                'execution_name': EXEC,
                'wait_for': WAIT_FOR,
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'date': '2026-01-01',
                'pipeline_execution': 'p-run-2026-01-01-fail1',
                'pipeline_name': 'p',
            })

            for asset in ('asset_a', 'asset_b'):
                subs_tbl.put_item(Item={
                    'dependency_key': f'asset:{asset}',
                    'subscriber': 'consumer',
                    'wait_token': 'tok',
                    'execution_name': EXEC,
                    'subscription_type': 'asset',
                })

            tok_repo = dal.TokensRepo('pipeline-tokens-fail')
            subs_repo = dal.SubscriptionsRepo('dep-subscriptions-fail')
            mocker.patch.object(index, 'tokens_repo', tok_repo)
            mocker.patch.object(index, 'subscriptions_repo', subs_repo)
            mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
            # Signal fails — B1 keeps the subscription; B3 must not clean siblings
            mocker.patch.object(index, '_send_task_success', return_value='failed')
            mocker.patch.object(index, '_write_signal_failure_warn')

            index.handler(
                {'outlets': [{'name': 'asset_a', 'uri': 's3://b/a'}], 'event_time': '2026-01-01T08:00:00Z'},
                None,
            )

            # Both subscriptions must survive (B1 keeps asset_a; B3 must not touch asset_b)
            for asset in ('asset_a', 'asset_b'):
                assert subs_tbl.get_item(
                    Key={'dependency_key': f'asset:{asset}', 'subscriber': 'consumer'}
                ).get('Item') is not None, f"{asset} subscription must survive when signal failed"

    def test_mixed_spec_or_sibling_deleted(self, monkeypatch, mocker, modules):
        """wait_for=[x, a | b]: when asset_a signals, asset_b sibling cleaned up."""
        from moto import mock_aws
        import boto3

        index, dal = modules
        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal, '_dynamodb', None)

        EXEC = 'consumer-2026-01-01-mixed1'
        TOKEN = 'tok-mixed'
        # Mixed spec: x is a plain AND dep, then OR group for a and b
        WAIT_FOR = json.dumps([
            {'name': 'asset_x'},
            {'operator': 'OR', 'assets': [{'name': 'asset_a'}, {'name': 'asset_b'}]},
        ])

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-mixed',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            subs_tbl = dynamodb.create_table(
                TableName='dep-subscriptions-mixed',
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

            # Mixed spec → no gate (wait_for_assets absent)
            tokens_tbl.put_item(Item={
                'execution_name': EXEC,
                'wait_for': WAIT_FOR,
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'date': '2026-01-01',
                'pipeline_execution': 'p-run-2026-01-01-mixed',
                'pipeline_name': 'p',
            })

            for asset in ('asset_a', 'asset_b', 'asset_x'):
                subs_tbl.put_item(Item={
                    'dependency_key': f'asset:{asset}',
                    'subscriber': 'consumer',
                    'wait_token': TOKEN,
                    'execution_name': EXEC,
                    'subscription_type': 'asset',
                })

            tok_repo = dal.TokensRepo('pipeline-tokens-mixed')
            subs_repo = dal.SubscriptionsRepo('dep-subscriptions-mixed')
            mocker.patch.object(index, 'tokens_repo', tok_repo)
            mocker.patch.object(index, 'subscriptions_repo', subs_repo)
            mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')
            mocker.patch.object(index, '_send_task_success', return_value='sent')

            index.handler(
                {'outlets': [{'name': 'asset_a', 'uri': 's3://b/a'}], 'event_time': '2026-01-01T08:00:00Z'},
                None,
            )

            # asset_a deleted (normal path)
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer'}
            ).get('Item') is None

            # asset_b (OR sibling) deleted by B3
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer'}
            ).get('Item') is None, "OR sibling asset_b must be cleaned up"

            # asset_x (plain AND member) deleted by B3 as well (it's in the flatten set)
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_x', 'subscriber': 'consumer'}
            ).get('Item') is None, "Plain AND member asset_x must also be cleaned up"
