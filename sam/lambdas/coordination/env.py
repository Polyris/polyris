"""Environment and AWS clients of the coordination Lambdas (ADR #130 §10).

Module names in this Lambda are deliberately distinct from other Lambdas'
(`config`, `dal`, ...): a combined `pytest sam/lambdas` run shares one
interpreter, and same-named modules would shadow each other.
"""

import os
from datetime import timedelta

TOKENS_TABLE = os.environ.get("TOKENS_TABLE", "pipeline-tokens")
SUBSCRIPTIONS_TABLE = os.environ.get("SUBSCRIPTIONS_TABLE", "dependency-subscriptions")
ASSET_EVENTS_TABLE = os.environ.get("ASSET_EVENTS_TABLE", "asset-events")
PIPELINES_TABLE = os.environ.get("PIPELINES_TABLE", "pipeline-registry")

PIPELINE_EXECUTION_INDEX = "pipeline-execution-index"

#: Asset facts written this recently are also read from the base table with a
#: consistent read, closing the partition index's propagation lag (ADR #130 §4).
RECENT_WRITES = timedelta(minutes=15)

_dynamodb = None
_stepfunctions = None


def dynamodb():
    """Lazily created DynamoDB resource (imports stay cheap for tests)."""
    global _dynamodb
    if _dynamodb is None:
        import boto3

        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def stepfunctions():
    """Lazily created Step Functions client."""
    global _stepfunctions
    if _stepfunctions is None:
        import boto3

        _stepfunctions = boto3.client("stepfunctions")
    return _stepfunctions
