"""
Console API Utilities

Shared utility functions for all route modules:
- DynamoDB helpers (scan_all, query_all)
- Type conversion helpers (safe_int, safe_param_int)
- Task execution helpers
"""

import json
import os
import re
from decimal import Decimal
from datetime import datetime, timezone
from typing import Dict, Any, List

from polyris.coordination.records import DAG_SNAPSHOT_PREFIX

from config import (
    s3, lambda_client,
    EXECUTION_NAME_PATTERN
)
from constants import Limits
from logger import log


def now_iso() -> str:
    """UTC timestamp in ISO-8601. Single source for `created_at`/`*_at` stamps
    across repos and routes (DRY, Core Principle #1)."""
    return datetime.now(timezone.utc).isoformat()


def resolve_pagerduty(item: Dict) -> None:
    """Resolve a PagerDuty incident when a human makes a decision (skip/fail/success).

    Non-blocking: failures are logged but never break the calling action.
    Invokes the notify Lambda's ``resolve_pagerduty`` action (ADR #103 Stage 2 —
    the posting moved out of the pagerduty_resolver helper SFN). The Lambda reads
    the routing key from alert_config by pipeline_name and resolves the incident
    keyed by the same dedup_key (pipeline/task/date) the alert used.
    """
    notify_arn = os.environ.get('NOTIFY_FUNCTION_ARN')
    if not notify_arn:
        return

    pipeline_name = item.get('pipeline_name', '')
    if not pipeline_name:
        return

    try:
        lambda_client.invoke(
            FunctionName=notify_arn,
            InvocationType='Event',   # fire-and-forget; resolve is best-effort
            Payload=json.dumps({
                'action': 'resolve_pagerduty',
                'pipeline_name': pipeline_name,
                'failure': {
                    'pipeline_name': pipeline_name,
                    'task_name': item.get('task_name', ''),
                    'execution_name': item.get('execution_name', ''),
                    'date': item.get('date', ''),
                },
            }).encode('utf-8'),
        )
        log.info("resolve_pagerduty", "Triggered", item=item.get('execution_name', 'unknown'))
    except Exception as e:
        log.error("resolve_pagerduty", "Failed (non-blocking)", error=str(e))


def is_internal_record(execution_name: str) -> bool:
    """Check if a pipeline-tokens record is internal/special (not a real task execution).

    Internal records use _ prefix:
    - _run_{pipeline_execution} — the run row (its pauses, stop mark and requests); _run_{…}#input — its input
    - _pause_{pipeline_execution} — a 1.2 run's pause; 1.3 writes none
    - _notify_warn_{kind}_{execution_name} — infrastructure warning (visible in Notifications bell)

    Canonical output store records (``output#pipeline#task#date``, written by the
    run_task wrapper for upstream reads) are also internal — they carry a task's
    result, not a task execution, and must not appear in execution/task listings.

    Split-out task_input records (``input#pipeline#task#date``, new in 1.0.0)
    are the twin of ``output#...`` — same rationale: Console preview data, not
    a task execution row. Missing this filter would leak input# rows into
    All Tasks / Runs listings.

    DAG snapshots (``dag_snapshot::{pipeline_execution}``, written by the
    pipeline state machine when a run starts) are internal too. They carry the
    run's ``pipeline_execution``, so every query of a run's rows returns them:
    unfiltered, a snapshot showed as a task without a status, and the sort of
    /api/execution-children failed on it.

    All loops iterating pipeline-tokens items MUST call this and skip True results.
    """
    return (
        execution_name.startswith('_')
        or execution_name.startswith('output#')
        or execution_name.startswith('input#')
        or execution_name.startswith(DAG_SNAPSHOT_PREFIX)
    )


def is_backfill_record(item: dict) -> bool:
    """Check if a pipeline-tokens item is a Backfill record (not a regular execution).

    Backfill records introduced in v0.78 (per ADR #51) live in the same
    pipeline-tokens table, distinguished by:
    - ``pipeline_name`` = ``"_polyris_bulk_backfill"`` sentinel
    - ``record_type`` = ``"backfill"``

    Either marker is sufficient (defense in depth against partial writes).
    Backfill records must be excluded from execution-listing endpoints
    and counters; only their child executions (linked by ``backfill_id``)
    represent real work.
    """
    if item.get('record_type') == 'backfill':
        return True
    if item.get('pipeline_name') == '_polyris_bulk_backfill':
        return True
    return False


def should_skip_token_row(item: dict) -> bool:
    """Combined filter for pipeline-tokens iteration: skip if internal OR backfill record.

    Use this in every loop that iterates pipeline-tokens rows when the
    intent is "process real pipeline executions". Replaces the older
    pattern of calling ``is_internal_record(item.get('execution_name'))``
    alone, which misses Backfill records.
    """
    return is_internal_record(item.get('execution_name', '')) or is_backfill_record(item)


def is_execution_name(name: str) -> bool:
    """
    Check if the given name is a full execution_name.
    
    execution_name format: {task_name}-{YYYY-MM-DD}-{short_id}
    where short_id is 8-20 characters from pipeline_execution.
    
    task_name format: just the task name without date suffix
    
    Examples:
        'extract-2024-01-15-abc123xyz' -> True  (valid execution_name)
        'import-2024-data-special'     -> False (task name with 'data' not date)
        'load_daily'                   -> False (plain task name)
        'task-2024-01-15'              -> False (no short_id suffix)
    """
    return bool(EXECUTION_NAME_PATTERN.match(name))


def retrieve_result(result: Any) -> Any:
    """
    Retrieve result from S3 if it's a Claim Check reference.
    
    If result contains _s3_ref, fetches the actual data from S3.
    Otherwise returns the result as-is.
    """
    if not isinstance(result, dict) or '_s3_ref' not in result:
        return result
    
    try:
        s3_uri = result['_s3_ref']
        # Parse s3://bucket/key
        parts = s3_uri.replace('s3://', '').split('/', 1)
        bucket = parts[0]
        key = parts[1] if len(parts) > 1 else ''
        
        response = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(response['Body'].read().decode('utf-8'))
    except Exception as e:
        log.error("retrieve_result", "Error retrieving result from S3", error=str(e))
        # Return the reference with error info
        return {**result, '_retrieval_error': str(e)}


def parse_wait_before(value) -> int:
    """Parse wait_before value from DynamoDB (handles Decimal, int, str)."""
    if value is None:
        return 0
    try:
        if isinstance(value, Decimal):
            return int(value)
        elif isinstance(value, (int, float)):
            return int(value)
        elif isinstance(value, str) and value.strip():
            return int(float(value))
        return 0
    except (ValueError, TypeError):
        return 0


def safe_int(value, default: int = 0) -> int:
    """Safely parse int from DynamoDB value (handles Decimal, int, str, None, '')."""
    if value is None:
        return default
    try:
        if isinstance(value, Decimal):
            return int(value)
        elif isinstance(value, (int, float)):
            return int(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return default
            return int(float(stripped))
        return default
    except (ValueError, TypeError):
        return default


def safe_param_int(params: Dict, key: str, default: int, max_val: int = None) -> int:
    """Safely parse int from URL query params. Returns default on invalid input."""
    if not params:
        return default
    try:
        value = int(params.get(key, default))
        if max_val is not None:
            return min(value, max_val)
        return value
    except (ValueError, TypeError):
        return default


def scan_all(table, max_items: int = None, **scan_kwargs) -> List[Dict]:
    """
    Scan DynamoDB table with automatic pagination.
    Returns all items across all pages, up to max_items limit.
    
    Use this instead of inline pagination loops to reduce code duplication.
    For scans that need processing during pagination (e.g., aggregation,
    early exit on condition), use inline loops instead.
    
    Args:
        table: boto3 DynamoDB Table resource
        max_items: Safety limit to prevent runaway scans (default: Limits.MAX_SCAN_ITEMS)
        **scan_kwargs: Additional arguments to pass to scan() - FilterExpression, etc.
    
    Returns:
        List of all items from all pages (up to max_items)
    
    Example:
        items = scan_all(
            table,
            max_items=5000,
            FilterExpression=Attr('status').eq('failed')
        )
    """
    if max_items is None:
        max_items = Limits.MAX_SCAN_ITEMS
    
    items = []
    last_key = None
    
    while len(items) < max_items:
        if last_key:
            scan_kwargs['ExclusiveStartKey'] = last_key
        
        response = table.scan(**scan_kwargs)
        items.extend(response.get('Items', []))
        
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
    
    if len(items) >= max_items:
        log.warn("scan_all", "Hit max_items limit", max_items=max_items)
    
    return items[:max_items]


def query_all(table, max_items: int = None, **query_kwargs) -> List[Dict]:
    """
    Query DynamoDB table with automatic pagination.
    Returns all items across all pages, up to max_items limit.
    
    Use this instead of inline pagination loops to reduce code duplication.
    For queries that need processing during pagination (e.g., dedupe, 
    early exit on condition), use inline loops instead.
    
    Args:
        table: boto3 DynamoDB Table resource
        max_items: Safety limit to prevent runaway queries (default: Limits.MAX_SCAN_ITEMS)
        **query_kwargs: Arguments to pass to query() - IndexName, KeyConditionExpression, etc.
    
    Returns:
        List of all items from all pages (up to max_items)
    
    Example:
        items = query_all(
            table,
            max_items=1000,
            IndexName='date-pipeline-index',
            KeyConditionExpression=Key('date').eq('2026-01-28')
        )
    """
    if max_items is None:
        max_items = Limits.MAX_SCAN_ITEMS
    
    items = []
    last_key = None
    
    while len(items) < max_items:
        if last_key:
            query_kwargs['ExclusiveStartKey'] = last_key
        
        response = table.query(**query_kwargs)
        items.extend(response.get('Items', []))
        
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
    
    if len(items) >= max_items:
        log.warn("query_all", "Hit max_items limit", max_items=max_items)
    
    return items[:max_items]


# ============================================
# Input Validation Helpers
# ============================================

def validate_date(date_str: str) -> tuple:
    """
    Validate date string format (YYYY-MM-DD).
    
    Args:
        date_str: Date string to validate
    
    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if not date_str:
        return False, "Date is required"
    
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return False, "Date must be in YYYY-MM-DD format"
    
    # Validate actual date
    try:
        datetime.strptime(date_str, '%Y-%m-%d')
        return True, None
    except ValueError:
        return False, "Invalid date"


_SCHEMA_COLUMN_DEFAULTS = {
    'description': '',
    'nullable':       True,
    'primary_key':    False,
    'partition_key':  False,
    'unique':         False,
    'default':        None,
}


def dict_schema_richness(schema: List[Dict]) -> int:
    """Score how 'rich' a serialized (dict-form) schema is.

    Mirror of `polyris.schema.dict_schema_richness` — kept in sync because
    the Lambda doesn't ship with the SDK package (Principle #1: single
    source of truth lives in `polyris.schema`, this is its on-the-wire
    twin). Used by `_build_assets_from_pipelines` when the same asset is
    declared in multiple pipelines: higher score wins, ties keep the
    first declaration.

    Each column scores 1, plus 1 per constraint key set to a non-default
    value (PK, partition, NOT NULL, UNIQUE, default, description).
    """
    score = 0
    for col in schema or ():
        if not isinstance(col, dict):
            continue  # be defensive — registry may contain malformed entries
        score += 1
        for key, default in _SCHEMA_COLUMN_DEFAULTS.items():
            value = col.get(key)
            if key in col and value != default and value is not None:
                score += 1
    return score


def parse_task_config(raw) -> Dict:
    """Parse the task_config DDB field (JSON string or dict) into a Python dict.

    task_config is stored as a JSON string by the registration SFN.  Older
    boto3 paths or direct dict injection (e.g. in tests) may produce a dict.
    Returns an empty dict on any parse failure so callers can safely .get().
    """
    if raw is None:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def resolve_max_retries(item: Dict):
    """Return the task's current max_retries, preferring the EE-editable
    top-level field over the registration-written task_config blob.

    EE update_task_config writes 'max_retries' directly on the DDB row;
    the registration SFN embeds 'retries' inside the 'task_config' JSON
    string.  The top-level field is always more recent when present.
    Returns None if neither source is set.
    """
    top = item.get('max_retries')
    if top is not None:
        return safe_int(top)
    return parse_task_config(item.get('task_config')).get('retries')
