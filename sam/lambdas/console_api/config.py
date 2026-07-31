"""
Console API Configuration

AWS clients, environment variables, and constants.
Centralized configuration for all route modules.

Uses lazy initialization for AWS clients to improve testability
and prevent boto3 initialization errors during imports.
"""

import os
import re
from typing import Dict

# ============================================
# AWS Clients (lazy initialization)
# ============================================
_dynamodb = None
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


def _get_sfn():
    """Lazy init Step Functions client."""
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client('stepfunctions')
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
SUBSCRIPTIONS_TABLE = os.environ.get('SUBSCRIPTIONS_TABLE', 'dep-subscriptions')
ASSET_EVENTS_TABLE = os.environ.get('ASSET_EVENTS_TABLE', 'asset-events')
QUEUED_EVENTS_TABLE = os.environ.get('QUEUED_EVENTS_TABLE', 'queued-asset-events')
TASK_EVENTS_TABLE = os.environ.get('TASK_EVENTS_TABLE', 'task-events')
# Personal Access Token store (ADR #65). Dedicated table, not pipeline-tokens.
API_TOKENS_TABLE = os.environ.get('API_TOKENS_TABLE', 'api-tokens')
# Optional: only set when circuit-breaker monitoring is enabled in this deployment.
# Empty string means feature disabled (health check returns HEALTHY/not-configured).
CIRCUIT_BREAKER_TABLE = os.environ.get('CIRCUIT_BREAKER_TABLE', '')
RESULTS_BUCKET = os.environ.get('RESULTS_BUCKET', '')

# SFN Helpers
NOTIFY_DEPENDENTS_HELPER_ARN = os.environ.get('NOTIFY_DEPENDENTS_HELPER_ARN', '')
NOTIFY_ASSET_CONSUMERS_SFN_ARN = os.environ.get('NOTIFY_ASSET_CONSUMERS_SFN_ARN', '')
BULK_BACKFILL_ARN = os.environ.get('BULK_BACKFILL_ARN', '')

# ============================================
# Constants
# ============================================
# Regex pattern for execution_name format: {task_name}-{YYYY-MM-DD}-{short_id}
# short_id can be any length (typically 1-20 chars from pipeline_execution)
# This pattern ensures: prefix-DATE-suffix (date not at start/end alone)
EXECUTION_NAME_PATTERN = re.compile(r'.+-\d{4}-\d{2}-\d{2}-.{8,}$')
