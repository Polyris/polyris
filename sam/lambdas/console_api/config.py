"""
Console API Configuration

AWS clients, environment variables, and constants.
Centralized configuration for all route modules.

Uses lazy initialization for AWS clients to improve testability
and prevent boto3 initialization errors during imports.
"""

import os
from typing import Dict

# ============================================
# AWS Clients (lazy initialization)
# ============================================
_dynamodb = None
_dynamodb_client = None
_sfn = None
_lambda = None
_logs = None
_cloudwatch = None
_s3 = None
_glue = None


def _get_dynamodb():
    """Lazy init DynamoDB resource."""
    global _dynamodb
    if _dynamodb is None:
        import boto3
        _dynamodb = boto3.resource('dynamodb')
    return _dynamodb


def _get_dynamodb_client():
    """Lazy init a low-level DynamoDB client, for code that sends typed attributes
    (``polyris.coordination.spec_store``). A resource's ``meta.client`` would
    serialize them a second time."""
    global _dynamodb_client
    if _dynamodb_client is None:
        import boto3
        _dynamodb_client = boto3.client('dynamodb')
    return _dynamodb_client


#: Step Functions calls fail fast, well inside API Gateway's 29 s (Principle #53): botocore's
#: standard retry mode with two attempts, a 2 s connect and a 4 s read timeout, so one
#: call gives up within 12 s. Every console call is a control-plane call (list, start, stop);
#: none waits for an execution, and none asks about a run (ADR #137 §5). The defaults (a 60 s
#: read, legacy retries) let one slow call outlast the request.
SFN_CLIENT_CONFIG = {'retries': {'mode': 'standard', 'total_max_attempts': 2},
                     'connect_timeout': 2, 'read_timeout': 4}


def _get_sfn():
    """Lazy init Step Functions client."""
    global _sfn
    if _sfn is None:
        import boto3
        from botocore.config import Config
        _sfn = boto3.client('stepfunctions', config=Config(**SFN_CLIENT_CONFIG))
    return _sfn


def _get_lambda():
    """Lazy init Lambda client (used to invoke the notify Lambda for directed
    actions like PagerDuty resolve — ADR #103 Stage 2)."""
    global _lambda
    if _lambda is None:
        import boto3
        _lambda = boto3.client('lambda')
    return _lambda


def _get_logs():
    """Lazy init CloudWatch Logs client."""
    global _logs
    if _logs is None:
        import boto3
        _logs = boto3.client('logs')
    return _logs


def _get_cloudwatch():
    """Lazy init CloudWatch client."""
    global _cloudwatch
    if _cloudwatch is None:
        import boto3
        _cloudwatch = boto3.client('cloudwatch')
    return _cloudwatch


def _get_s3():
    """Lazy init S3 client."""
    global _s3
    if _s3 is None:
        import boto3
        _s3 = boto3.client('s3')
    return _s3


def _get_glue():
    """Lazy init Glue client for the Lambda's default region.

    Used by the default path of GET /api/assets/{name}/glue-schema. For
    cross-region asset references see `get_glue_client(region)`.
    """
    global _glue
    if _glue is None:
        import boto3
        _glue = boto3.client('glue')
    return _glue


# Per-region cache for cross-region Glue calls. Lambda containers are reused
# across invocations so memoizing avoids reinstantiating boto3 clients on
# every request — same shape as `_get_glue` but keyed by region. Empty/None
# region returns the default-region client (so callers can pass through
# whatever was stored on the asset without branching).
_glue_by_region: Dict[str, object] = {}


def get_glue_client(region: str = ''):
    """Return a Glue boto3 client for the given region.

    Pass empty string or None to get the Lambda's default-region client
    (same instance as `glue`). Otherwise creates and caches a region-pinned
    client. Used by the schema-fetch route to honour `Asset.glue_region`.
    """
    if not region:
        return _get_glue()
    cached = _glue_by_region.get(region)
    if cached is None:
        import boto3
        cached = boto3.client('glue', region_name=region)
        _glue_by_region[region] = cached
    return cached


# Lazy proxy objects that look like boto3 clients but init on first use
class _LazyDynamoDB:
    def __getattr__(self, name):
        return getattr(_get_dynamodb(), name)
    
    def Table(self, table_name):
        return _get_dynamodb().Table(table_name)


class _LazyDynamoDBClient:
    def __getattr__(self, name):
        return getattr(_get_dynamodb_client(), name)


class _LazySFN:
    def __getattr__(self, name):
        return getattr(_get_sfn(), name)


class _LazyLambda:
    def __getattr__(self, name):
        return getattr(_get_lambda(), name)


class _LazyLogs:
    def __getattr__(self, name):
        return getattr(_get_logs(), name)


class _LazyCloudWatch:
    def __getattr__(self, name):
        return getattr(_get_cloudwatch(), name)


class _LazyS3:
    def __getattr__(self, name):
        return getattr(_get_s3(), name)


class _LazyGlue:
    def __getattr__(self, name):
        return getattr(_get_glue(), name)


# Export lazy proxies (same interface as before)
dynamodb = _LazyDynamoDB()
dynamodb_client = _LazyDynamoDBClient()
sfn = _LazySFN()
lambda_client = _LazyLambda()
logs = _LazyLogs()
cloudwatch = _LazyCloudWatch()
s3 = _LazyS3()
glue = _LazyGlue()

# ============================================
# Environment Variables
# ============================================
TABLE_NAME = os.environ.get('DYNAMODB_TABLE', 'pipeline-executions')
PIPELINES_TABLE = os.environ.get('PIPELINES_TABLE', 'pipeline-registry')
ASSET_EVENTS_TABLE = os.environ.get('ASSET_EVENTS_TABLE', 'asset-events')
# How long an asset fact lives; the same value run_task's fact writes use.
ASSET_EVENT_TTL_DAYS = int(os.environ.get('ASSET_EVENT_TTL_DAYS', '365'))
TASK_EVENTS_TABLE = os.environ.get('TASK_EVENTS_TABLE', 'task-events')
# Personal Access Token store (ADR #65, API tokens). Dedicated table, not pipeline-tokens.
API_TOKENS_TABLE = os.environ.get('API_TOKENS_TABLE', 'api-tokens')
# Task specs (ADR #137 §8): polyris-deploy writes them; the console reads them (dal.specs_repo).
SPECS_TABLE = os.environ.get('SPECS_TABLE', 'pipeline-specs')
# Optional: only set when circuit-breaker monitoring is enabled in this deployment.
# Empty string means feature disabled (health check returns HEALTHY/not-configured).
CIRCUIT_BREAKER_TABLE = os.environ.get('CIRCUIT_BREAKER_TABLE', '')
RESULTS_BUCKET = os.environ.get('RESULTS_BUCKET', '')

# Step Functions
BULK_BACKFILL_ARN = os.environ.get('BULK_BACKFILL_ARN', '')
