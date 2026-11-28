"""
The platform's rows that the notify Lambda reads (ADR #103): the registry's alert config, and the task row a
failure names (ADR #142 §5).

The single source of truth for per-pipeline alert config is the registry table's
``alert_config`` attribute. Both the batch fan-out (index.py) and the directed
actions (interactive Slack, live/resolve PagerDuty) read it from here by
pipeline name, so neither depends on the config being threaded through the SFN
input. Reading via a boto3 *resource* table means DynamoDB's typed attributes are
already deserialized to a plain dict (``{slack: {webhook_param: …}}``) — no
``.M``/``.S`` unwrapping needed at the call sites.
"""

import os
from typing import Any, Dict

import boto3
from botocore.config import Config
from logger import log

#: Every AWS call fails well inside the function's 30 s timeout (Principle #53): botocore's defaults (ten
#: DynamoDB attempts, a 60 s read) outlast it, and a function that times out cannot say what failed.
AWS_CONFIG = Config(retries={'mode': 'standard', 'total_max_attempts': 3}, connect_timeout=2, read_timeout=5)

_dynamodb = None


def _resource():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'),
                                   config=AWS_CONFIG)
    return _dynamodb


def registry_table():
    """Lazy DynamoDB *resource* table for the pipeline registry (holds
    alert_config). A resource (not a low-level client) auto-deserializes typed
    attributes to plain Python on read."""
    return _resource().Table(os.environ.get('PIPELINES_TABLE', 'pipeline-registry'))


def get_alert_config(pipeline_name: str) -> Dict[str, Any]:
    """Read alert_config for a pipeline as a plain dict; degrade to empty on any
    miss or error so a failed read simply sends nothing external."""
    try:
        resp = registry_table().get_item(Key={'pipeline_name': pipeline_name})
        item = resp.get('Item') or {}
        cfg = item.get('alert_config') or {}
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        log.error('registry', 'Failed to read alert_config',
                  pipeline=pipeline_name, error_type=type(e).__name__, error=str(e))
        return {}


def tokens_table():
    """Lazy DynamoDB *resource* table for the task rows (``pipeline-tokens``)."""
    return _resource().Table(os.environ.get('TOKENS_TABLE', 'pipeline-tokens'))


def get_task_row(execution_name: str) -> Dict[str, Any]:
    """The task row under ``execution_name``, consistently, or {} when there is none. A failed read raises: the
    caller decides that an alert it cannot check is not sent, and says so."""
    resp = tokens_table().get_item(Key={'execution_name': execution_name}, ConsistentRead=True)
    item = resp.get('Item') or {}
    return item if isinstance(item, dict) else {}
