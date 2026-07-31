"""
Pipeline-registry reads for the notify Lambda (ADR #103).

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
from logger import log

_dynamodb = None


def registry_table():
    """Lazy DynamoDB *resource* table for the pipeline registry (holds
    alert_config). A resource (not a low-level client) auto-deserializes typed
    attributes to plain Python on read."""
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource('dynamodb', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    name = os.environ.get('PIPELINES_TABLE', 'pipeline-registry')
    return _dynamodb.Table(name)


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
