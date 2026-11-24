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
#: Push consumers: pipelines with `schedule=[assets]` (AssetFanout only).
ASSET_SUBSCRIPTIONS_TABLE = os.environ.get("ASSET_SUBSCRIPTIONS_TABLE", "asset-subscriptions")
#: The task specs (ADR #137 §8), which Start_Run and the dispatcher read.
SPECS_TABLE = os.environ.get("SPECS_TABLE", "pipeline-specs")
#: Each task's timeline, where the dispatcher writes its decisions (ADR #137 §1).
TASK_EVENTS_TABLE = os.environ.get("TASK_EVENTS_TABLE", "task-events")
#: The state machine the dispatcher starts for a ready row (ADR #137 §1).
RUN_TASK_ARN = os.environ.get("RUN_TASK_ARN", "run-task")
#: The function the asset-events stream feeds; the sweeper checks its event source mapping.
ASSET_FANOUT_FUNCTION = os.environ.get("ASSET_FANOUT_FUNCTION", "asset-fanout")
#: The function the tasks table's stream feeds (the dispatcher); the sweeper checks its event source mapping.
DISPATCHER_FUNCTION = os.environ.get("DISPATCHER_FUNCTION", "reconcile")


#: Asset facts written this recently are also read from the base table with a
#: consistent read, closing the partition index's propagation lag (ADR #130 §4).
RECENT_WRITES = timedelta(minutes=15)

#: Every AWS call fails fast, well inside the Lambda's own timeout: botocore's
#: standard retry mode with three attempts, a 2 s connect and a 5 s read timeout.
#: Its defaults (ten DynamoDB attempts, a 60 s read) outlast a 30 s reconcile, and
#: a Lambda that times out cannot report CoordinationTransient (ADR #130 §6.3).
CLIENT_RETRIES = {"mode": "standard", "total_max_attempts": 3}
CONNECT_TIMEOUT_SECONDS = 2
READ_TIMEOUT_SECONDS = 5

_dynamodb = None
_dynamodb_client = None
_stepfunctions = None
_lambda = None


def _config():
    from botocore.config import Config

    return Config(retries=dict(CLIENT_RETRIES), connect_timeout=CONNECT_TIMEOUT_SECONDS,
                  read_timeout=READ_TIMEOUT_SECONDS)


def dynamodb():
    """Lazily created DynamoDB resource (imports stay cheap for tests)."""
    global _dynamodb
    if _dynamodb is None:
        import boto3

        _dynamodb = boto3.resource("dynamodb", config=_config())
    return _dynamodb


def dynamodb_client():
    """Lazily created low-level DynamoDB client, for code that sends DynamoDB's typed values itself
    (``spec_store``): a resource's ``meta.client`` would serialize them a second time."""
    global _dynamodb_client
    if _dynamodb_client is None:
        import boto3

        _dynamodb_client = boto3.client("dynamodb", config=_config())
    return _dynamodb_client


def lambda_():
    """Lazily created Lambda client (the sweeper's self-check)."""
    global _lambda
    if _lambda is None:
        import boto3

        _lambda = boto3.client("lambda", config=_config())
    return _lambda


def stepfunctions():
    """Lazily created Step Functions client."""
    global _stepfunctions
    if _stepfunctions is None:
        import boto3

        _stepfunctions = boto3.client("stepfunctions", config=_config())
    return _stepfunctions
