"""End-to-end AND-gate test: wait_for=[a, b] with moto DynamoDB.

Acceptance criterion from TASKS.md task 1.20:
  - asset_a arrives → no signal sent; subscription for a deleted; assets_ready_set={'a'}
  - asset_b arrives → signal sent; subscription for b deleted; assets_ready_set={'a','b'}

Principle #14: mock at the external boundary (boto3 via moto). The DAL expressions
run against real DynamoDB semantics (moto); only _send_task_success is replaced to
isolate AND-gate routing from the SFN boto3 call.
"""

import pytest


class TestWaitForAndSemantics:
    """Full AND-gate integration with moto DynamoDB.

    Verifies that the DynamoDB StringSet gate (_coordinate_ready_check +
    add_to_assets_ready_set_and_get) correctly withholds the signal until all
    required assets have arrived, and that subscriptions are deleted at the
    right point (after credit for not-last-asset; after signal for last-asset).
    """

    def test_first_asset_does_not_signal_second_asset_does(self, monkeypatch, mocker):
        """wait_for=[a, b]: asset_a → no signal; asset_b → signal."""
        from moto import mock_aws
        import boto3
        import dal as dal_module
        from dal import SubscriptionsRepo, TokensRepo
        import index

        monkeypatch.setenv('AWS_DEFAULT_REGION', 'us-east-1')
        monkeypatch.setattr(dal_module, '_dynamodb', None)

        EXEC = 'consumer_task-2026-01-01-and12345'
        TOKEN = 'fake-sfn-token-and12345'
        EVENT_TIME = '2026-01-01T08:00:00Z'

        with mock_aws():
            dynamodb = boto3.resource('dynamodb', region_name='us-east-1')

            tokens_tbl = dynamodb.create_table(
                TableName='pipeline-tokens-and',
                KeySchema=[{'AttributeName': 'execution_name', 'KeyType': 'HASH'}],
                AttributeDefinitions=[{'AttributeName': 'execution_name', 'AttributeType': 'S'}],
                BillingMode='PAY_PER_REQUEST',
            )
            subs_tbl = dynamodb.create_table(
                TableName='dep-subscriptions-and',
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

            # Seed task record — AND gate requires both assets
            tokens_tbl.put_item(Item={
                'execution_name': EXEC,
                'wait_for_assets': {'asset_a', 'asset_b'},
                'dependencies': '[]',
                'trigger_rule': 'all_success',
                'wait_for': '["asset_a", "asset_b"]',
                'date': '2026-01-01',
                'pipeline_execution_short': 'run-2026-01-01-and1234',
                'pipeline_execution': 'my-pipeline-run-2026-01-01-and1234',
                'pipeline_name': 'my-pipeline',
            })

            # Seed subscriptions for both assets
            for asset in ('asset_a', 'asset_b'):
                subs_tbl.put_item(Item={
                    'dependency_key': f'asset:{asset}',
                    'subscriber': 'consumer_task',
                    'wait_token': TOKEN,
                    'execution_name': EXEC,
                    'subscription_type': 'asset',
                })

            # Wire DAL repos to the moto tables
            tok_repo = TokensRepo('pipeline-tokens-and')
            subs_repo = SubscriptionsRepo('dep-subscriptions-and')
            mocker.patch.object(index, 'tokens_repo', tok_repo)
            mocker.patch.object(index, 'subscriptions_repo', subs_repo)
            mocker.patch.object(index, 'EVALUATE_DEPS_LAMBDA', '')

            # Mock _send_task_success — isolates AND-gate routing from SFN boto3 call
            mock_send = mocker.MagicMock(return_value='sent')
            mocker.patch.object(index, '_send_task_success', new=mock_send)

            # --- Round 1: asset_a arrives ---
            result_a = index.handler(
                {'outlets': [{'name': 'asset_a', 'uri': 's3://bucket/a'}], 'event_time': EVENT_TIME},
                None,
            )

            mock_send.assert_not_called()
            assert result_a['notified'] == 0, "No signal when AND gate is not yet satisfied"

            # Subscription for asset_a deleted (asset credited; nothing to retry later)
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_a', 'subscriber': 'consumer_task'}
            ).get('Item') is None, "Subscription asset_a must be deleted after first arrival"

            # Subscription for asset_b still present
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer_task'}
            ).get('Item') is not None, "Subscription asset_b must remain until asset_b arrives"

            # DDB reflects partial assets_ready_set
            rec_mid = tokens_tbl.get_item(Key={'execution_name': EXEC}).get('Item')
            assert rec_mid.get('assets_ready_set') == {'asset_a'}

            # --- Round 2: asset_b arrives ---
            mock_send.reset_mock()
            result_b = index.handler(
                {'outlets': [{'name': 'asset_b', 'uri': 's3://bucket/b'}], 'event_time': EVENT_TIME},
                None,
            )

            mock_send.assert_called_once_with(
                TOKEN,
                asset_name='asset_b',
                asset_uri='s3://bucket/b',
                event_time=EVENT_TIME,
            )
            assert result_b['notified'] == 1
            assert result_b['subscribers'] == ['consumer_task']

            # Subscription for asset_b deleted after signal
            assert subs_tbl.get_item(
                Key={'dependency_key': 'asset:asset_b', 'subscriber': 'consumer_task'}
            ).get('Item') is None, "Subscription asset_b must be deleted after signal"

            # DDB reflects full set
            rec_final = tokens_tbl.get_item(Key={'execution_name': EXEC}).get('Item')
            assert rec_final.get('assets_ready_set') == {'asset_a', 'asset_b'}
