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

from config import (
    sfn, s3, lambda_client,
    EXECUTION_NAME_PATTERN
)
from dal.task_events_repo import task_events_repo
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
    - _pause_{pipeline_execution} — pause state
    - _notify_warn_{execution_name} — infrastructure warning (visible in Notifications bell)

    Canonical output store records (``output#pipeline#task#date``, written by the
    run_task wrapper for upstream reads) are also internal — they carry a task's
    result, not a task execution, and must not appear in execution/task listings.

    All loops iterating pipeline-tokens items MUST call this and skip True results.
    """
    return execution_name.startswith('_') or execution_name.startswith('output#')


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


def record_manual_decision(execution_name: str, decision: str, reason: str = '', item: Dict = None) -> None:
    """
    Record MANUAL_DECISION event in task_events table for timeline/debugging.
    
    If the task had an error (from failed execution), also records a TASK_FINISHED
    event so the error is preserved in history even after skip/mark_success.
    
    Uses deterministic sort keys (execution_name+decision hash) so retries
    overwrite the same item instead of creating duplicates.
    
    Args:
        execution_name: Task execution name
        decision: Action taken (skip, fail, stop, restart)
        reason: Optional reason for the decision
        item: Task item from DynamoDB (for task_run_id, parent_execution_id, error)
    """
    import hashlib
    
    try:
        # Extract IDs from item if available
        task_run_id = item.get('task_run_id', execution_name) if item else execution_name
        parent_execution_id = item.get('parent_execution_id', item.get('pipeline_execution', '')) if item else ''
        task_name = item.get('task_name', execution_name.split('-')[0]) if item else execution_name.split('-')[0]
        
        # TTL = 30 days
        ttl = int((datetime.now(timezone.utc).timestamp())) + (30 * 24 * 60 * 60)
        
        # Deterministic suffix from execution_name + decision (retry-safe)
        det_suffix = hashlib.md5(f"{execution_name}-{decision}".encode()).hexdigest()[:6]
        
        # If task had an error, emit TASK_FINISHED (failed) event first
        # This preserves the error in timeline even when task is skipped/marked success
        error_str = item.get('error', '') if item else ''
        if error_str:
            error_event_time = f"{datetime.now(timezone.utc).isoformat()}#25#{det_suffix}"
            
            # Truncate error for summary
            error_summary = error_str[:500] + '...' if len(error_str) > 500 else error_str
            
            task_events_repo.put({
                'task_run_id': task_run_id,
                'event_time': error_event_time,
                'event_type': 'TASK_FINISHED',
                'execution_name': execution_name,
                'parent_execution_id': parent_execution_id,
                'task_name': task_name,
                'status': 'failed',
                'error_summary': error_summary,
                'ttl': ttl
            })
        
        # Record MANUAL_DECISION event
        event_time = f"{datetime.now(timezone.utc).isoformat()}#30#{det_suffix}"
        
        task_events_repo.put({
            'task_run_id': task_run_id,
            'event_time': event_time,
            'event_type': 'MANUAL_DECISION',
            'execution_name': execution_name,
            'parent_execution_id': parent_execution_id,
            'task_name': task_name,
            'decision': decision,
            'reason': reason or f'Manual {decision} via UI',
            'ttl': ttl
        })
    except Exception as e:
        log.error("utils", "Error recording MANUAL_DECISION", error=str(e))
        # Don't fail the action if event recording fails


def compute_pipeline_execution_short(pipeline_execution: str) -> str:
    """
    Compute pipeline_execution_short from full pipeline_execution.
    
    Mirrors the JSONata logic in dependency_wrapper:
    - Take last 20 characters
    - Remove '.' and ':' characters (Step Functions ARN compatibility)
    
    Args:
        pipeline_execution: Full pipeline execution ID
    
    Returns:
        Sanitized short ID (max 20 chars), or empty string if no input
    """
    if not pipeline_execution:
        return ''
    
    # Take last 20 chars
    short = pipeline_execution[-20:] if len(pipeline_execution) > 20 else pipeline_execution
    
    # Remove . and : (same as wrapper JSONata: $replace($replace(..., '.', ''), ':', ''))
    short = short.replace('.', '').replace(':', '')
    
    return short


def ensure_pipeline_execution_short(pipeline_execution: str, existing_short: str = '') -> str:
    """
    Ensure pipeline_execution_short has a valid value.
    
    Uses existing value if present, otherwise computes from pipeline_execution.
    This is critical for notify_dependents to work correctly.
    
    Args:
        pipeline_execution: Full pipeline execution ID
        existing_short: Existing short value (if any)
    
    Returns:
        Valid pipeline_execution_short (max 20 chars), or empty string if no input
    """
    if existing_short:
        return existing_short
    return compute_pipeline_execution_short(pipeline_execution)


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


def stop_task_executions(item: Dict, error: str, cause: str) -> None:
    """
    Stop all Step Functions executions related to a task.
    Stops both wrapper and run_task_helper to prevent orphaned executions.
    
    Args:
        item: Task item from DynamoDB containing execution ARNs
        error: Error message for the stop operation
        cause: Cause description for the stop operation
    """
    wrapper_arn = item.get('wrapper_execution_arn')
    run_task_helper_arn = item.get('run_task_helper_arn')
    
    # Stop wrapper execution
    if wrapper_arn:
        try:
            sfn.stop_execution(executionArn=wrapper_arn, error=error, cause=cause)
        except sfn.exceptions.ExecutionDoesNotExist:
            pass  # Already finished
        except Exception as e:
            log.error("stop_task_executions", "Failed to stop wrapper", error=str(e))
    
    # Stop run_task_helper execution (prevents orphaned helper that could overwrite status)
    if run_task_helper_arn:
        try:
            sfn.stop_execution(executionArn=run_task_helper_arn, error=error, cause=cause)
        except sfn.exceptions.ExecutionDoesNotExist:
            pass  # Already finished
        except Exception as e:
            log.error("stop_task_executions", "Failed to stop run_task_helper", error=str(e))


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


def validate_execution_name(name: str) -> tuple:
    """
    Validate execution_name format.
    
    Args:
        name: Execution name to validate
    
    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if not name:
        return False, "Execution name is required"
    
    if len(name) > Limits.EXECUTION_NAME_MAX_LENGTH:
        return False, f"Execution name too long (max {Limits.EXECUTION_NAME_MAX_LENGTH} chars)"
    
    # Check for invalid characters
    if not re.match(r'^[\w\-]+$', name):
        return False, "Execution name contains invalid characters"
    
    return True, None


def validate_pipeline_name(name: str) -> tuple:
    """
    Validate pipeline_name format.
    
    Args:
        name: Pipeline name to validate
    
    Returns:
        (is_valid: bool, error_message: str or None)
    """
    if not name:
        return False, "Pipeline name is required"
    
    if len(name) > 64:
        return False, "Pipeline name too long (max 64 chars)"
    
    if not re.match(r'^[\w\-]+$', name):
        return False, "Pipeline name contains invalid characters"
    
    return True, None


def validate_required_fields(data: Dict, required: List[str]) -> tuple:
    """
    Validate that required fields are present in data.
    
    Args:
        data: Dictionary to validate
        required: List of required field names
    
    Returns:
        (is_valid: bool, missing_fields: List[str])
    """
    if not data:
        return False, required
    
    missing = [field for field in required if not data.get(field)]
    return len(missing) == 0, missing


# Schema column defaults — kept in sync with `polyris.schema._COLUMN_DEFAULTS`.
# Backend works with the serialized dict form (omit-on-default), so any key
# present here that holds a non-default value counts as a "rich" constraint.
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
