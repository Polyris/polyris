"""
Shared pytest fixtures for console_api tests.

Provides mocked AWS clients and helper factories for API Gateway events.

Environment variables are set before any imports to ensure boto3 initializes correctly.
"""

import os

# Set AWS environment variables BEFORE importing anything that uses boto3
# This is required because boto3 reads these at import time
os.environ.setdefault('AWS_DEFAULT_REGION', 'us-east-1')
os.environ.setdefault('AWS_ACCESS_KEY_ID', 'testing')
os.environ.setdefault('AWS_SECRET_ACCESS_KEY', 'testing')
os.environ.setdefault('DYNAMODB_TABLE', 'test-pipeline-executions')
os.environ.setdefault('PIPELINES_TABLE', 'test-pipeline-registry')
os.environ.setdefault('ASSET_EVENTS_TABLE', 'test-asset-events')
os.environ.setdefault('SUBSCRIPTIONS_TABLE', 'test-subscriptions')
os.environ.setdefault('TASK_EVENTS_TABLE', 'test-task-events')
os.environ.setdefault('API_TOKENS_TABLE', 'test-api-tokens')
os.environ.setdefault('SLS_STAGE', 'test')

import pytest
import json
from datetime import datetime, timezone


@pytest.fixture
def mock_dynamodb(mocker):
    """Mock DynamoDB resource."""
    mock = mocker.MagicMock()
    mocker.patch('console_api.config.dynamodb', mock)
    return mock


@pytest.fixture
def mock_executions_repo(mocker):
    """Mock executions repo (pipeline-executions table)."""
    mock = mocker.MagicMock()
    mocker.patch('dal.executions_repo', mock)
    return mock


@pytest.fixture
def mock_pipelines_repo(mocker):
    """Mock pipelines repo (pipeline-registry table)."""
    mock = mocker.MagicMock()
    mocker.patch('dal.pipelines_repo', mock)
    return mock


@pytest.fixture
def mock_sfn(mocker):
    """Mock Step Functions client."""
    mock = mocker.MagicMock()
    mocker.patch('console_api.config.sfn', mock)
    return mock


@pytest.fixture
def mock_s3(mocker):
    """Mock S3 client."""
    mock = mocker.MagicMock()
    mocker.patch('console_api.config.s3', mock)
    return mock


@pytest.fixture
def mock_logs(mocker):
    """Mock CloudWatch Logs client."""
    mock = mocker.MagicMock()
    mocker.patch('console_api.config.logs', mock)
    return mock


@pytest.fixture
def mock_cloudwatch(mocker):
    """Mock CloudWatch client."""
    mock = mocker.MagicMock()
    mocker.patch('console_api.config.cloudwatch', mock)
    return mock


@pytest.fixture
def sample_pipeline():
    """Sample pipeline registry item."""
    return {
        'pipeline_name': 'test-pipeline',
        'sfn_arn': 'arn:aws:states:us-east-1:123456789012:stateMachine:test-pipeline',
        'schedule': 'rate(1 day)',
        'dag': '{"nodes": [], "edges": []}',
        'registered_at': '2024-01-15T10:00:00Z',
        'paused': False
    }


@pytest.fixture
def sample_task_item():
    """Sample task execution item from pipeline-executions table."""
    return {
        'execution_name': 'extract-2024-01-15-abc123xy',
        'task_name': 'extract',
        'pipeline_name': 'test-pipeline',
        'pipeline_execution': 'test-2024-01-15-abc123xyz',
        'pipeline_execution_short': 'abc123xy',
        'parent_execution_id': 'test-2024-01-15-abc123xyz',
        'task_run_id': 'extract-2024-01-15-abc123xy',
        'status': 'running',
        'date': '2024-01-15',
        'started_at': '2024-01-15T10:00:00Z',
        'attempt': 1,
        'wrapper_execution_arn': 'arn:aws:states:us-east-1:123456789012:execution:wrapper:extract-123',
        'run_task_helper_arn': 'arn:aws:states:us-east-1:123456789012:execution:helper:extract-123'
    }


@pytest.fixture
def sample_asset():
    """Sample asset registry item."""
    return {
        'asset_name': 'datalake/bronze/users',
        'pipeline_name': 'user-etl',
        'producing_task': 'extract_users',
        'created_at': '2024-01-15T10:00:00Z',
        'consumers': ['analytics-pipeline/transform_users']
    }


@pytest.fixture
def api_event():
    """Factory for API Gateway HTTP API v2 events."""
    def _make_event(
        method: str = 'GET',
        path: str = '/',
        body: dict = None,
        query_params: dict = None,
        headers: dict = None
    ):
        event = {
            'rawPath': path,
            'requestContext': {
                'http': {
                    'method': method,
                    'path': path
                },
                'requestId': 'test-request-123'
            },
            'headers': headers or {},
            'queryStringParameters': query_params,
            'body': json.dumps(body) if body else None,
            'isBase64Encoded': False
        }
        return event
    return _make_event


@pytest.fixture
def mock_table(mocker):
    """Factory for mocked DynamoDB table with common operations."""
    def _make_table(items: list = None, scan_items: list = None, query_items: list = None):
        table = mocker.MagicMock()
        
        # get_item returns single item
        if items:
            def get_item_side_effect(Key):
                for item in items:
                    # Match on execution_name (most common key)
                    if Key.get('execution_name') == item.get('execution_name'):
                        return {'Item': item}
                    # Match on pipeline_name
                    if Key.get('pipeline_name') == item.get('pipeline_name'):
                        return {'Item': item}
                    # Match on asset_name
                    if Key.get('asset_name') == item.get('asset_name'):
                        return {'Item': item}
                return {'Item': None}
            table.get_item.side_effect = get_item_side_effect
        else:
            table.get_item.return_value = {'Item': None}
        
        # scan returns all items
        table.scan.return_value = {
            'Items': scan_items or items or [],
            'Count': len(scan_items or items or [])
        }
        
        # query returns filtered items
        table.query.return_value = {
            'Items': query_items or [],
            'Count': len(query_items or [])
        }
        
        # put_item and update_item return success
        table.put_item.return_value = {}
        table.update_item.return_value = {}
        table.delete_item.return_value = {}
        
        return table
    return _make_table


@pytest.fixture
def freeze_time(mocker):
    """Factory to freeze datetime.now() for consistent tests."""
    def _freeze(year=2024, month=1, day=15, hour=10, minute=0, second=0):
        frozen_time = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
        
        def mock_now(tz=None):
            return frozen_time
        
        mocker.patch('datetime.datetime.now', mock_now)
        return frozen_time
    return _freeze
