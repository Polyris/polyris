"""Shared task action utilities for Console API.

This module contains common functions used by both UI routes (tasks.py) 
and Slack routes (slack.py) to avoid code duplication.

Functions:
- notify_dependents_via_sfn: Invoke notify_dependents_helper SFN
- record_facts_for_manual_success: Record asset facts for Mark Successful
  specifically (§7c); the asset-events stream wakes their consumers
- get_terminal_statuses: Get set of terminal statuses
- build_condition_expression_values: Build ExpressionAttributeValues for terminal check
"""
import json
import uuid
from typing import Dict, List, Set

from config import (
    sfn,
    ASSET_EVENT_TTL_DAYS,
    NOTIFY_DEPENDENTS_HELPER_ARN,
)
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


def record_facts_for_manual_success(
    outlets: List[Dict],
    task_name: str,
    pipeline_name: str,
    partition_key: str,
    events_repo,
    asset_event_ttl_days: int = ASSET_EVENT_TTL_DAYS,
) -> None:
    """Record the facts of a task marked successful.

    Mark Successful is the one manual action that claims real work happened,
    so it records each outlet's fact the way run_task's Record_Asset_Event
    does (ADR #130 §1). The asset-events stream does the rest, as for any
    other fact: it wakes the tasks waiting on the asset and starts the
    pipelines scheduled on it. Skip, Fail and Stop record nothing: they claim
    no output.

    Best-effort per outlet: one outlet's failure is logged and does not stop
    the others, nor the manual action's own response.

    Args:
        outlets: dicts with at least a 'name' key (optional 'uri' and
            'granularity'), as stored in the pipeline registry's dag_metadata.
        task_name: the task whose outlets these are.
        pipeline_name: the owning pipeline (source_dag).
        partition_key: the partition of the marked run — the task row's
            ``date`` — never the day the action happens (B-M1).
        events_repo: the asset-events DAL repo (dal.asset_events_repo).
        asset_event_ttl_days: how long the facts live.
    """
    for outlet in outlets or []:
        asset_name = outlet.get('name')
        if not asset_name:
            continue
        try:
            events_repo.write_fact(
                asset_name=asset_name,
                partition_key=partition_key,
                granularity=outlet.get('granularity'),
                cascade='auto',
                source_dag=pipeline_name,
                source_task=task_name,
                uri=outlet.get('uri', ''),
                ttl_days=asset_event_ttl_days,
            )
        except Exception as e:
            log.error("record_facts_for_manual_success", "Failed to record asset fact",
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
