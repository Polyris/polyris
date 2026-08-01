"""Shared task action utilities for Console API.

This module contains common functions used by both UI routes (tasks.py) 
and Slack routes (slack.py) to avoid code duplication.

Functions:
- notify_dependents_via_sfn: Invoke notify_dependents_helper SFN
- notify_asset_consumers_for_manual_success: Record asset event + notify
  push-triggered consumers, for Mark Successful specifically (§7c)
- get_terminal_statuses: Get set of terminal statuses
- build_condition_expression_values: Build ExpressionAttributeValues for terminal check
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Set

from config import sfn, NOTIFY_DEPENDENTS_HELPER_ARN, NOTIFY_ASSET_CONSUMERS_SFN_ARN
from constants import TASK_TERMINAL_STATUSES
from logger import log


def get_terminal_statuses() -> Set[str]:
    """Get the set of terminal statuses.
    
    Returns TASK_TERMINAL_STATUSES which includes:
    - success, failed, skipped, upstream_failed, aborted
    
    Use this instead of hardcoding terminal sets to ensure consistency.
    """
    return TASK_TERMINAL_STATUSES


def is_terminal_status(status: str) -> bool:
    """Check if a status is terminal."""
    return status in TASK_TERMINAL_STATUSES


def build_condition_expression_values(base_values: Dict = None) -> Dict:
    """Build ExpressionAttributeValues dict with all terminal statuses.
    
    This ensures ConditionExpression checks include ALL terminal statuses
    (derived from the canonical TASK_TERMINAL_STATUSES — never a
    hand-maintained duplicate list, which previously drifted: it was
    missing ':succeeded').
    
    Args:
        base_values: Optional dict to merge with terminal status values
        
    Returns:
        Dict with terminal status values merged with base_values
    """
    terminal_values = {f':{s}': s for s in TASK_TERMINAL_STATUSES}
    
    if base_values:
        terminal_values.update(base_values)
    
    return terminal_values


# Standard ConditionExpression for checking NOT in terminal state. Derived
# from the canonical TASK_TERMINAL_STATUSES (sorted for a deterministic,
# readable expression string) rather than hand-typed, so it can never drift
# from build_condition_expression_values' own :placeholders.
TERMINAL_CONDITION_EXPRESSION = 'NOT #s IN ({})'.format(
    ', '.join(f':{s}' for s in sorted(TASK_TERMINAL_STATUSES))
)

# For recovery operations (skip/mark_success on failed tasks) - only block on already-resolved states
RESOLVED_CONDITION_EXPRESSION = 'NOT #s IN (:success, :skipped)'


def build_resolved_expression_values(base_values: Dict = None) -> Dict:
    """Build ExpressionAttributeValues for resolved-only check (success, skipped)."""
    resolved_values = {
        ':success': 'success',
        ':skipped': 'skipped',
    }
    if base_values:
        return {**resolved_values, **base_values}
    return resolved_values


def notify_dependents_via_sfn(
    task_name: str,
    status: str,
    date: str,
    pipeline_execution_short: str,
    pipeline_execution: str = ''
) -> bool:
    """Invoke notify_dependents_helper SFN to signal waiting tasks.
    
    Replaces EventBridge-based notify_dependents with direct SFN call.
    Returns True on success, False on failure (logged but non-blocking).
    
    Args:
        task_name: Name of the completed task
        status: Terminal status (success, failed, skipped, aborted)
        date: Execution date (YYYY-MM-DD)
        pipeline_execution_short: Short pipeline execution ID
        pipeline_execution: Full pipeline execution ID (optional)
        
    Returns:
        True if SFN started successfully, False otherwise
    """
    if not NOTIFY_DEPENDENTS_HELPER_ARN:
        log.warn("notify_dependents_via_sfn", "NOTIFY_DEPENDENTS_HELPER_ARN not configured")
        return False
    
    try:
        # Use deterministic name to prevent duplicate notifications on retry
        exec_id = uuid.uuid4().hex[:8]
        safe_task = task_name.replace('/', '-').replace('.', '-')[:40]
        notify_name = f"notify-{safe_task}-{status}-{date}-{exec_id}"
        sfn.start_execution(
            stateMachineArn=NOTIFY_DEPENDENTS_HELPER_ARN,
            name=notify_name,
            input=json.dumps({
                'task_name': task_name,
                'status': status,
                'date': date,
                'pipeline_execution_short': pipeline_execution_short,
                'pipeline_execution': pipeline_execution
            })
        )
        log.info("notify_dependents_via_sfn", "Started SFN", task=task_name, status=status)
        return True
    except Exception as e:
        log.error("notify_dependents_via_sfn", "Failed to start SFN", task=task_name, error=str(e))
        return False


def notify_asset_consumers_for_manual_success(
    outlets: List[Dict],
    task_name: str,
    pipeline_name: str,
    date: str,
    events_repo,
    asset_event_ttl_days: int = 30,
) -> None:
    """Record asset events and notify push-triggered consumer pipelines —
    the same two steps the wrapper's own Record_Asset_Event +
    Notify_Asset_Consumers_SFN states perform on a normal successful
    completion (run_task/sfn.tpl.json's Emit_Asset_Events Map), invoked here
    for Mark Successful specifically: it's the one manual action that
    explicitly claims real work happened (its own doc line: "work completed,
    verified via logs/S3"), so a downstream pipeline scheduled on this asset
    (push model, schedule=[asset]) should be told the same way a normal
    completion would tell it. Skip/Fail/Stop correctly do not call this —
    they make no claim that anything was actually produced.

    Best-effort per outlet: one outlet's failure (DDB or SFN) is logged and
    does not stop the remaining outlets from being processed, matching the
    wrapper's own per-item MaxConcurrency Map semantics where one item
    failing fails that item, not the whole task's manual-resolution response.

    Args:
        outlets: list of dicts with at least a 'name' key (and optional
            'uri'), as stored in the pipeline registry's dag_metadata nodes.
        task_name: the task whose outlets these are.
        pipeline_name: the owning pipeline (source_dag).
        date: execution date (YYYY-MM-DD).
        events_repo: the asset-events DAL repo (dal.asset_events_repo). Passed
            in rather than imported so this shared utils module keeps no hard
            boto3 dependency beyond what callers already have — the DAL owns
            the table handle, so no caller needs a raw resource.
        asset_event_ttl_days: TTL for the asset event record, matching the
            wrapper template's own ${asset_event_ttl_days} default.
    """
    if not outlets:
        return
    if not NOTIFY_ASSET_CONSUMERS_SFN_ARN:
        log.warn("notify_asset_consumers_for_manual_success", "NOTIFY_ASSET_CONSUMERS_SFN_ARN not configured")

    now = datetime.now(timezone.utc)
    ttl = int(now.timestamp()) + (asset_event_ttl_days * 24 * 60 * 60)

    for outlet in outlets:
        asset_name = outlet.get('name')
        if not asset_name:
            continue
        uri = outlet.get('uri', '')

        try:
            events_repo.put({
                'asset_name': asset_name,
                'event_time': now.isoformat(),
                'uri': uri,
                'source_task': task_name,
                'source_dag': pipeline_name,
                'execution_date': date,
                'ttl': ttl,
            })
        except Exception as e:
            log.error("notify_asset_consumers_for_manual_success", "Failed to record asset event",
                      asset_name=asset_name, task_name=task_name, error=str(e))

        if not NOTIFY_ASSET_CONSUMERS_SFN_ARN:
            continue
        try:
            exec_id = uuid.uuid4().hex[:8]
            safe_asset = asset_name.replace('/', '-').replace('.', '-')[:40]
            sfn.start_execution(
                stateMachineArn=NOTIFY_ASSET_CONSUMERS_SFN_ARN,
                name=f"notify-asset-{safe_asset}-{date}-{exec_id}",
                input=json.dumps({
                    'asset_name': asset_name,
                    'execution_date': date,
                    'source_task': task_name,
                    'source_dag': pipeline_name,
                    'cascade_all': False,
                }),
            )
            log.info("notify_asset_consumers_for_manual_success", "Started SFN",
                     asset_name=asset_name, task_name=task_name)
        except Exception as e:
            log.error("notify_asset_consumers_for_manual_success", "Failed to start SFN",
                      asset_name=asset_name, task_name=task_name, error=str(e))


def extract_pipeline_execution_short(item: Dict, execution_name: str) -> str:
    """Extract pipeline_execution_short from item or execution_name.
    
    Args:
        item: DynamoDB item with task info
        execution_name: Task execution name (format: task_name-date-short_id)
        
    Returns:
        Pipeline execution short ID
    """
    # First priority: use stored value
    pipeline_execution_short = item.get('pipeline_execution_short', '')
    if pipeline_execution_short:
        return pipeline_execution_short
    
    # Second priority: compute from pipeline_execution using same logic as wrapper
    pipeline_execution = item.get('pipeline_execution', '')
    if pipeline_execution:
        # Import here to avoid circular dependency
        from utils import compute_pipeline_execution_short
        return compute_pipeline_execution_short(pipeline_execution)
    
    # Last resort: extract from execution_name suffix
    if execution_name.count('-') >= 3:
        # Format: task_name-YYYY-MM-DD-short_id
        return execution_name.rsplit('-', 1)[-1]
    
    return ''
