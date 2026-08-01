"""Task routes for Console API.

Handles task operations:
- get_all_tasks: List all tasks with filters
- get_task_config: Get task configuration
- skip_task: Skip a waiting/failed task
- fail_task: Mark task as failed
- mark_success: Mark task as successful manually
- stop_task: Stop a running task (can be restarted)
- restart_task: Restart a stopped/failed task
- get_task_events: Get task timeline events
"""
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Dict, Optional

from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError, BotoCoreError

from config import sfn
from dal import asset_events_repo, executions_repo, pipelines_repo
from dal.task_events_repo import task_events_repo
from constants import Limits, TaskStatus, TASK_WAITING_STATUSES, TASK_SETTLED_STATUSES, TASK_SUCCESS_STATUSES, TASK_TERMINAL_STATUSES
from feed import feed_dates, is_older, page_by_started_at, pipeline_rows_before
from response import cors_response, safe_parse_body
from logger import log
from utils import (
    should_skip_token_row,
    is_execution_name, safe_int, safe_param_int,
    stop_task_executions, record_manual_decision, ensure_pipeline_execution_short,
    resolve_pagerduty, retrieve_result
)
from task_actions import (
    notify_dependents_via_sfn,
    notify_asset_consumers_for_manual_success,
    is_terminal_status,
    build_condition_expression_values,
    TERMINAL_CONDITION_EXPRESSION,
    RESOLVED_CONDITION_EXPRESSION,
    build_resolved_expression_values
)



def resolve_task_item(
    task_name: str, 
    date: str, 
    pipeline_execution: str = ''
) -> tuple:
    """
    Resolve task item from DynamoDB by task_name or execution_name.
    
    Handles both formats:
    - Full execution_name (e.g., 'extract-2024-01-15-abc123xyz')
    - Task name only (requires GSI lookup with date)
    
    Uses pagination for GSI queries to handle large datasets.
    Falls back to GSI if direct lookup fails.
    
    Args:
        task_name: Task name or full execution_name
        date: Date for GSI lookup (YYYY-MM-DD)
        pipeline_execution: Optional pipeline execution filter
        
    Returns:
        (item, execution_name) tuple, or (None, None) if not found
    """
    item = None
    execution_name = task_name
    
    # Try direct lookup first if it looks like execution_name
    if is_execution_name(task_name):
        item = executions_repo.get(task_name)
        if item:
            return item, task_name
        # Fall through to GSI lookup if not found
    
    # GSI lookup with pagination
    filter_expr = Attr('task_name').eq(task_name)
    if pipeline_execution:
        filter_expr = (
            Attr('task_name').eq(task_name) & 
            Attr('pipeline_execution').eq(pipeline_execution)
        )
    
    query_kwargs = {
        'KeyConditionExpression': Key('date').eq(date),
        'FilterExpression': filter_expr,
        # Only fetch fields needed for discovery (not full record)
        'ProjectionExpression': 'execution_name, started_at, task_name, pipeline_execution',
    }
    
    # Paginate through all pages (bounded by max_pages) so a match on an
    # earlier page never hides a MORE RECENT match on a later one — the date
    # partition spans every pipeline's tasks that day, so a task_name match
    # can appear on any page, and this function's job is to find the most
    # recent one (see the sort below), not just the first one encountered.
    last_key = None
    all_items = []
    max_pages = 10  # Safety limit

    for _ in range(max_pages):
        if last_key:
            query_kwargs['ExclusiveStartKey'] = last_key
        
        response = executions_repo.query_by_date_raw(**query_kwargs)
        items = response.get('Items', [])
        all_items.extend(items)
        
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            break
    
    if all_items:
        # Get the most recent one if multiple
        best = sorted(all_items, key=lambda x: x.get('started_at', ''), reverse=True)[0]
        execution_name = best.get('execution_name', task_name)
        # Fetch full record by primary key (GSI may not have all fields)
        item = executions_repo.get(execution_name)
        if item:
            return item, execution_name
    
    return None, None


# Task-status groupings are canonical (polyris → TASK_SETTLED_STATUSES /
# TASK_SUCCESS_STATUSES); never re-list them inline.


def _reconcile_orphaned_tasks(tasks):
    """Reconcile tasks stuck in non-terminal status when their execution already failed.
    
    When an orchestrator SFN times out or fails, tasks like waiting_delay
    remain in DynamoDB with their old status. This checks SFN execution
    status and marks orphaned tasks as 'aborted'.
    """
    # Find non-terminal tasks grouped by (pipeline_name, pipeline_execution)
    pending_execs = {}  # {(pipeline_name, pipeline_execution): [task_indices]}
    for i, task in enumerate(tasks):
        if task['status'] not in TASK_SETTLED_STATUSES and task.get('pipeline_execution'):
            key = (task.get('pipeline_name', ''), task['pipeline_execution'])
            pending_execs.setdefault(key, []).append(i)
    
    if not pending_execs:
        return tasks
    
    # Get SFN ARNs from registry for unique pipeline names
    pipeline_arns = {}
    for pipeline_name in set(k[0] for k in pending_execs):
        try:
            item = pipelines_repo.get(pipeline_name)
            if item:
                pipeline_arns[pipeline_name] = item.get('sfn_arn', '') or item.get('arn', '')
        except (ClientError, BotoCoreError) as e:
            log.warn("_reconcile_orphaned_tasks", "Failed to load pipeline registry entry",
                        pipeline_name=pipeline_name, error=str(e))
    
    # Check execution status for each pending group
    failed_execs = set()
    for (pipeline_name, exec_name), _ in pending_execs.items():
        sm_arn = pipeline_arns.get(pipeline_name)
        if not sm_arn:
            continue
        
        # Construct execution ARN: stateMachine:name → execution:name:exec_name
        exec_arn = sm_arn.replace(':stateMachine:', ':execution:') + ':' + exec_name
        try:
            resp = sfn.describe_execution(executionArn=exec_arn)
            status = resp.get('status', '')
            if status in ('FAILED', 'TIMED_OUT', 'ABORTED'):
                failed_execs.add((pipeline_name, exec_name))
        except (ClientError, BotoCoreError) as e:
            log.warn("_reconcile_orphaned_tasks", "Failed to describe pipeline execution",
                        pipeline_name=pipeline_name, exec_name=exec_name, error=str(e))
    
    # Reconcile: mark non-terminal tasks in failed executions as 'aborted'
    # BUT skip tasks whose wrapper is still running (e.g. restarted tasks)
    if failed_execs:
        for (pipeline_name, exec_name), indices in pending_execs.items():
            if (pipeline_name, exec_name) in failed_execs:
                for i in indices:
                    # Check if task has a running wrapper (restart creates new wrapper)
                    wrapper_arn = tasks[i].get('wrapper_execution_arn', '')
                    if wrapper_arn:
                        try:
                            resp = sfn.describe_execution(executionArn=wrapper_arn)
                            if resp.get('status', '') == 'RUNNING':
                                continue  # Wrapper still active, not orphaned
                        except (ClientError, BotoCoreError) as e:
                            # Can't check wrapper — fall through to mark aborted.
                            # This is intentional: stale ARN or missing wrapper means
                            # the task has no live execution to continue from.
                            log.info("_reconcile_orphaned_tasks", "Wrapper describe failed, marking aborted",
                                     wrapper_arn=wrapper_arn, error=str(e))
                    tasks[i] = {**tasks[i], 'status': 'aborted'}
    
    return tasks


_TASKS_PROJECTION = ('execution_name, task_name, pipeline_name, #s, #d, started_at, '
                     'running_at, finished_at, dependencies, pipeline_execution, wait_for, '
                     'wrapper_execution_arn')
_TASKS_EXPR_NAMES = {'#s': 'status', '#d': 'date'}


def _format_task_row(item: Dict) -> Dict:
    """Project one pipeline-tokens row to a History tasks-feed row."""
    # Calculate duration if possible — use running_at (actual task start) not started_at (wrapper start)
    duration_ms = None
    actual_start = item.get('running_at') or item.get('started_at')
    if actual_start and item.get('finished_at'):
        try:
            start = datetime.fromisoformat(actual_start.replace('Z', '+00:00'))
            end = datetime.fromisoformat(item['finished_at'].replace('Z', '+00:00'))
            duration_ms = int((end - start).total_seconds() * 1000)
        except (ValueError, TypeError, AttributeError) as e:
            log.warn("get_all_tasks", "Bad timestamp on task; duration_ms left None",
                     execution_name=item.get('execution_name'),
                     actual_start=actual_start,
                     finished_at=item.get('finished_at'),
                     error=str(e))

    return {
        'execution_name': item.get('execution_name'),
        'task_name': item.get('task_name'),
        'pipeline_name': item.get('pipeline_name', 'unknown'),
        'status': item.get('status', 'unknown'),
        'date': item.get('date'),
        'started_at': item.get('started_at'),
        'running_at': item.get('running_at'),
        'finished_at': item.get('finished_at'),
        'duration_ms': duration_ms,
        'dependencies': item.get('dependencies', []),
        'wait_for': item.get('wait_for', '[]'),
        'pipeline_execution': item.get('pipeline_execution'),
        'wrapper_execution_arn': item.get('wrapper_execution_arn'),
        'notification_failed': item.get('notification_failed')
    }


def get_all_tasks(event: Dict) -> Dict:
    """Get all task instances across all pipelines.

    The other half of the History feed, and it pages exactly like ``/api/runs``: an
    opaque ``before`` cursor carrying a ``started_at``, ``next`` for the older page
    (see ``feed.py``). One dialect across both halves is the point — they list the
    same rows, so they should not disagree about how far back you can look.

    Source per filter, mirroring ``/api/runs`` (ADR #108): an explicit date is one
    indexed query; one pipeline reads ``pipeline-date-index`` and has no window;
    everything-newest-first shards on ``date`` and fans the window out per day.
    """
    params = event.get('queryStringParameters', {}) or {}
    
    # Optional filters
    status_filter = params.get('status')
    date_filter = params.get('date')
    pipeline_filter = params.get('pipeline')
    before = params.get('before', '')
    limit = safe_param_int(params, 'limit', Limits.TASKS_FEED_LIMIT, 500)

    status_expr = Attr('status').eq(status_filter) if status_filter else None

    def _keeps(item: Dict) -> bool:
        """Would this row survive to the page? Mirrors the filtering below, so the
        index read knows when it has pulled enough to fill one."""
        return (bool(item.get('task_name'))
                and not should_skip_token_row(item)
                and (not status_filter or item.get('status') == status_filter)
                and is_older(item, before))

    try:
        if date_filter:
            key_cond = Key('date').eq(date_filter)
            # Add pipeline filter to key condition if provided
            if pipeline_filter:
                key_cond = key_cond & Key('pipeline_name').eq(pipeline_filter)

            items = executions_repo.query_runs_by_date(
                date_filter,
                min_rows=Limits.TASKS_MIN_ROWS_PER_DATE,
                key_condition=key_cond,
                filter_expr=status_expr,
                projection=_TASKS_PROJECTION,
                expr_names=_TASKS_EXPR_NAMES,
            )
        elif pipeline_filter:
            # One pipeline, no date: pipeline-date-index, no window (ADR #108) — the
            # same read /api/runs uses, so both halves of History reach equally far
            # back. Replaces a full-table Scan: unbounded cost, and its arbitrary row
            # order made "the newest N" a lottery no cursor could page honestly.
            items = pipeline_rows_before(
                pipeline_filter, before, limit,
                count_fn=lambda rows: sum(1 for r in rows if _keeps(r)),
                projection=_TASKS_PROJECTION,
                expr_names=_TASKS_EXPR_NAMES,
            )
            # The index read takes no FilterExpression — its whole-date accounting
            # counts returned rows — so status is applied here instead.
            if status_filter:
                items = [i for i in items if i.get('status') == status_filter]
        else:
            items = []
            for date_str in feed_dates(before):
                try:
                    items.extend(executions_repo.query_runs_by_date(
                        date_str,
                        min_rows=Limits.FEED_MIN_ROWS_PER_DAY,
                        filter_expr=status_expr,
                        projection=_TASKS_PROJECTION,
                        expr_names=_TASKS_EXPR_NAMES,
                    ))
                except Exception as e:
                    log.error("get_all_tasks", "Error querying date", error=str(e), date_str=date_str)

        # Skip internal/special records (_pause_, _notify_warn_, output#) and Backfill
        # records, and pipeline-level/partial rows with no task_name — before paging,
        # so a page is a pageful of real rows.
        rows = [i for i in items if not should_skip_token_row(i) and i.get('task_name')]

        page, next_cursor = page_by_started_at(rows, before, limit)
        tasks = [_format_task_row(i) for i in page]

        # Reconcile: tasks stuck in non-terminal status but execution already failed.
        # After the cut — it costs an SFN describe per pending execution, and only the
        # rows being returned are worth paying for.
        tasks = _reconcile_orphaned_tasks(tasks)
        
        return cors_response(200, {
            'tasks': tasks,
            'count': len(tasks),
            # Opaque cursor for the next (older) page; None when nothing older exists.
            'next': next_cursor,
            'filters': {
                'status': status_filter,
                'date': date_filter,
                'pipeline': pipeline_filter
            }
        })
    except Exception as e:
        log.error("unknown", "Unexpected error", error=str(e))
        return cors_response(500, {'error': f'Failed to get tasks: {str(e)}'})


def get_task_config(task_name: str, event: Dict) -> Dict:
    """Get task configuration.
    
    Supports both execution_name (full) and task_name (requires date lookup).
    Uses pagination for GSI queries to handle large datasets.
    
    Returns config fields only (not runtime state like retry_attempts).
    """
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pipeline_execution = params.get('pipeline_execution', '')
    
    item, execution_name = resolve_task_item(task_name, date, pipeline_execution)
    
    if not item:
        item = {}
        execution_name = task_name
    
    return cors_response(200, {
        'task_name': task_name,
        'execution_name': execution_name,
        'config': {
            'timeout_seconds': safe_int(item.get('timeout_seconds'), 14400),
            'max_retries': safe_int(item.get('max_retries'), 3)
        },
        # Runtime state (read-only, for reference)
        'runtime': {
            'retry_attempts': safe_int(item.get('retry_attempts'), 0),
            'status': item.get('status', 'unknown')
        }
    })


def get_task_output(task_name: str, event: Dict) -> Dict:
    """Return a task's stored input and output.

    Reads the run-stable record (``output#pipeline#task#date``). ``output`` is the
    value the task returned; ``input`` is what it received — its upstream outputs and
    the injected run variables (upstream is omitted when the input exceeds ~25 KB).
    Large outputs offloaded to S3 (``_s3_ref``) are resolved transparently;
    ``truncated: true`` means the output exceeded the inline limit.
    """
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pipeline_execution = params.get('pipeline_execution', '')

    item, execution_name = resolve_task_item(task_name, date, pipeline_execution)
    resolved = item or {}
    pipeline_name = resolved.get('pipeline_name', '')
    # The output store keys on the plain task_name + run date; the route param may
    # be a full execution_name, so build the key from the resolved item's fields.
    plain_task = resolved.get('task_name', task_name)
    run_date = resolved.get('date', date)

    output = None
    task_input = None
    truncated = False
    if pipeline_name:
        key = f"output#{pipeline_name}#{plain_task}#{run_date}"
        try:
            store_item = executions_repo.get(key) or {}
            raw = store_item.get('result')
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get('_truncated'):
                    truncated = True
                else:
                    output = retrieve_result(parsed)
            raw_input = store_item.get('task_input')
            if raw_input:
                task_input = json.loads(raw_input)
        except (ClientError, BotoCoreError, ValueError) as e:
            log.error("get_task_output", "Error reading task input/output",
                      error=str(e), task_name=task_name)

    return cors_response(200, {
        'task_name': task_name,
        'execution_name': execution_name,
        'output': output,
        'input': task_input,
        'truncated': truncated,
    })


def get_task_events(execution_name: str, event: Dict) -> Dict:
    """
    Get real timeline events for a task.
    
    Returns all events from task_events table for debugging and timeline display.
    Events are ordered chronologically by event_time.
    """
    import urllib.parse
    execution_name = urllib.parse.unquote(execution_name)
    
    # Try to get task_run_id from tokens table first
    try:
        token_item = executions_repo.get(execution_name)
        task_run_id = (token_item or {}).get('task_run_id', execution_name)
    except Exception as e:
        # Falling back to execution_name as task_run_id keeps the events lookup
        # working (the GSI query below handles either id). Log so a real DDB
        # error doesn't get masked as "no events for this task".
        log.warn("get_task_events", "Token lookup failed; falling back to execution_name as task_run_id",
                 execution_name=execution_name, error=str(e))
        task_run_id = execution_name
    
    events = []
    
    # Try by task_run_id first
    try:
        events = task_events_repo.query_by_task_run_id(task_run_id)
    except Exception as e:
        log.error("get_task_events", "Error querying by task_run_id", error=str(e))
    
    # If no events found, try by execution_name GSI
    if not events:
        try:
            events = task_events_repo.query_by_execution_name(execution_name)
        except Exception as e:
            log.error("get_task_events", "Error querying by execution_name", error=str(e))
    
    # Format events for response
    formatted_events = []
    for evt in events:
        formatted_events.append({
            'event_type': evt.get('event_type', ''),
            'event_time': evt.get('event_time', '').split('#')[0],  # Remove sequence suffix
            'task_name': evt.get('task_name', ''),
            'status': evt.get('status', ''),
            'reason': evt.get('reason', ''),
            'decision': evt.get('decision', ''),  # For MANUAL_DECISION events
            'error_summary': evt.get('error_summary', ''),
            'dependencies': evt.get('dependencies', ''),
            'task_type': evt.get('task_type', ''),
            'task_arn': evt.get('task_arn', ''),
            'attempt': safe_int(evt.get('attempt'), 1)
        })
    
    return cors_response(200, {
        'execution_name': execution_name,
        'task_run_id': task_run_id,
        'events': formatted_events,
        'event_count': len(formatted_events)
    })



def _write_notify_warning(
    execution_name: str, task_name: str, pipeline_execution: str, pipeline_name: str, date: str, error: str
) -> None:
    """Write a warning record to pipeline-tokens when notify_dependents fails.

    This makes the failure visible in the Notifications bell in UI,
    which polls pipeline-tokens for status=failed records.
    """
    try:
        warn_key = f"_notify_warn_{execution_name}"
        executions_repo.put(
            {
                'execution_name': warn_key,
                'task_name': task_name,
                'pipeline_execution': pipeline_execution,
                'pipeline_name': pipeline_name,
                'date': date,
                'status': 'failed',
                'error': f'Downstream notification failed: {error}. Downstream tasks may be stuck waiting.',
                'finished_at': datetime.now(timezone.utc).isoformat(),
                'ttl': int(datetime.now(timezone.utc).timestamp()) + 86400,  # 24h TTL
            }
        )
    except Exception as e:
        log.error("_write_notify_warning", "Failed to write warning", error=str(e))


def retry_task(task_name: str, event: Dict) -> Dict:
    """Retry a failed/skipped task.

    DEPRECATED: This endpoint is deprecated. Use restart_task instead.
    This function now simply delegates to restart_task for backward compatibility.
    """
    # Delegate to restart_task for actual functionality
    return restart_task(task_name, event)


def _write_synthetic_output_marker(item: Dict, action_name: str, reason: str, date: str) -> None:
    """Write a synthetic marker to the canonical output store (the same
    output#{pipeline}#{task}#{date} key xcom.pull() and the console's
    Input/Output tab both read) when a task is manually resolved — Skip,
    Mark Successful, Mark Failed, or Stop — without ever actually running
    through the wrapper's Save_Canonical_Output state.

    Without this, a downstream task calling xcom.pull() on this task raises
    PullError ("no output stored ... did it return anything?") purely as a
    consequence of a manual decision made on an upstream task, through no
    fault of the downstream task's own logic.

    Conditional on 'result' NOT already existing: a genuine, real output from
    an earlier successful run of this same task/date (e.g. a same-day
    re-run, or a manual action taken after the task had already produced
    real output some other way) must never be overwritten by this synthetic
    marker — it only fills the gap when nothing real is there. Best-effort,
    matching every other status write in this codebase: a failure here must
    not block the manual action itself.
    """
    pipeline_name = item.get('pipeline_name', 'unknown')
    task_name = item.get('task_name', '')
    if not task_name:
        return
    key = f"output#{pipeline_name}#{task_name}#{date}"
    marker = json.dumps({
        '_manually_resolved': True,
        '_resolution': action_name,
        '_reason': reason,
    })
    ttl = int(datetime.now(timezone.utc).timestamp()) + (30 * 24 * 60 * 60)
    try:
        executions_repo.update(
            key,
            'SET task_name = :tn, #r = :result, #s = :status, '
            'updated_at = :ua, #ttl_field = if_not_exists(#ttl_field, :ttl)',
            condition_expr='attribute_not_exists(#r)',
            expr_names={'#r': 'result', '#s': 'status', '#ttl_field': 'ttl'},
            expr_values={
                ':tn': task_name,
                ':result': marker,
                ':status': action_name,
                ':ua': datetime.now(timezone.utc).isoformat(),
                ':ttl': ttl,
            },
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            log.info(
                "_write_synthetic_output_marker",
                "Real output already exists for this task/date — not overwriting",
                task_name=task_name,
            )
        else:
            log.warn(
                "_write_synthetic_output_marker", "Failed to write synthetic marker",
                error=str(e), task_name=task_name,
            )
    except BotoCoreError as e:
        log.warn(
            "_write_synthetic_output_marker", "Failed to write synthetic marker",
            error=str(e), task_name=task_name,
        )


def _lookup_dag_node(pipeline_name: str, task_name: str) -> Optional[Dict]:
    """Look up a single task's node from the pipeline registry's dag_metadata
    (per ADR #119's unified node builder — includes outlets, and, as of the
    restart data-loss fix, task_config too). Deploy-time DAG properties like
    these are never stored on the per-execution DynamoDB record, only here.

    Returns None on any failure (registry read, missing entry, malformed
    dag, or task not found) — callers treat this as best-effort and decide
    their own fallback.
    """
    if not pipeline_name:
        return None
    try:
        registry_item = pipelines_repo.get(pipeline_name)
        if not registry_item:
            return None
        dag_str = registry_item.get('dag', '{}')
        dag = json.loads(dag_str) if isinstance(dag_str, str) else dag_str
        return next((n for n in dag.get('nodes', []) if n.get('id') == task_name), None)
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError, ClientError, BotoCoreError) as e:
        log.warn("_lookup_dag_node", "Could not read node from registry",
                 task_name=task_name, pipeline_name=pipeline_name, error=str(e))
        return None


def _emit_asset_events_for_manual_success(item: Dict, task_name: str, date: str) -> None:
    """§7c: for Mark Successful specifically, look up this task's outlets
    from the pipeline registry (outlets are a deploy-time DAG property —
    never stored on the per-execution DynamoDB record, only in the
    registry's dag_metadata.nodes, per ADR #119's unified node builder) and
    notify push-triggered consumers the same way a normal completion would.

    Best-effort: any failure here (registry read, missing/malformed dag)
    is logged and does not affect the manual action's own success response —
    matching every other side-effect in this function's caller.
    """
    pipeline_name = item.get('pipeline_name', '')
    node = _lookup_dag_node(pipeline_name, task_name)
    outlets = node.get('outlets') if node else None
    if not outlets:
        return

    notify_asset_consumers_for_manual_success(
        outlets, task_name, pipeline_name, date, asset_events_repo,
    )


def _execute_task_action(
    task_name: str,
    event: Dict,
    *,
    action_name: str,
    target_status: str,
    use_resolved_check: bool,
    include_error_field: bool = False,
    stop_error: str,
    default_reason: str = None,
    default_stop_cause: str = None,
    callback_fn,
    success_message: str,
    skip_origin: str = None,
    emit_asset_events: bool = False,
) -> Dict:
    """Shared implementation for skip_task, fail_task, mark_success.

    Claim-first pattern: update DynamoDB status before executing side-effects.

    Args:
        action_name: For logging and record_manual_decision ('skip', 'fail', 'mark_success')
        target_status: New DynamoDB status ('skipped', 'failed', 'success')
        skip_origin: ADR #115 — written only by skip_task, as 'manual'. Distinguishes a
            human's explicit skip decision from the wrapper's rule-triggered skip
            (skip_origin='rule', written by notify_dependents' Update_Status_Skip when a
            trigger_rule's condition legitimately never occurs). all_success's
            skip-cascade treats only skip_origin='rule' as blocking; a manual skip does
            not cascade — a human tolerating one gap should not silently no-op an entire
            downstream success chain.
        use_resolved_check: True = block only resolved (success/skipped), allow recovery from failed.
                            False = block all terminal states.
        include_error_field: If True, include error field in UpdateExpression (for fail_task)
        stop_error: First arg to stop_task_executions ('Skipped', 'ManuallyFailed', 'ManuallySucceeded')
        default_reason: If set, extract 'reason' from body with this as default. None = no reason.
        default_stop_cause: Fallback stop_cause when reason is None (e.g. 'Task skipped via UI').
        callback_fn: callable(token, task_name, reason) that sends orchestration callback
        success_message: Template for 200 response message (use {execution_name})
        emit_asset_events: §7c — only mark_success passes True. Mark Successful is the
            one manual action that explicitly claims real work happened ("verified via
            logs/S3"), so a downstream pipeline scheduled on this task's outlets (push
            model) is notified the same way a normal completion would notify it.
            Skip/Fail/Stop correctly leave this False — they make no claim that
            anything was actually produced.
    """
    body, err = safe_parse_body(event)
    if err:
        return err
    date = body.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    pipeline_execution = body.get('pipeline_execution', '')
    reason = body.get('reason', default_reason) if default_reason else None

    # Resolve task using unified resolver (handles both formats with pagination)
    item, execution_name = resolve_task_item(task_name, date, pipeline_execution)

    if not item:
        log.warn(action_name, "Task not found", task_name=task_name, date=date)
        return cors_response(404, {'error': f'Task not found: {task_name} on {date}'})

    orchestration_token = item.get('orchestration_token')
    actual_task_name = item.get('task_name', task_name)
    pipeline_execution = item.get('pipeline_execution', '')
    pipeline_execution_short = item.get('pipeline_execution_short', '')
    current_status = item.get('status', '')

    # Check if action is blocked by current status
    if use_resolved_check:
        if current_status in TASK_SUCCESS_STATUSES:
            return cors_response(
                409,
                {
                    'error': f'Task already resolved: {current_status}',
                    'execution_name': execution_name,
                    'current_status': current_status,
                },
            )
        condition_expr = RESOLVED_CONDITION_EXPRESSION
        expr_values_fn = build_resolved_expression_values
    else:
        if is_terminal_status(current_status):
            return cors_response(
                409,
                {
                    'error': f'Task already in terminal state: {current_status}',
                    'execution_name': execution_name,
                    'current_status': current_status,
                },
            )
        condition_expr = TERMINAL_CONDITION_EXPRESSION
        expr_values_fn = build_condition_expression_values

    # Ensure pipeline_execution_short has a value (critical for notify_dependents)
    pipeline_execution_short = ensure_pipeline_execution_short(pipeline_execution, pipeline_execution_short)
    if not pipeline_execution_short:
        log.warn(action_name, "No pipeline_execution_short, event notification may fail", execution_name=execution_name)

    # Claim: update status FIRST (with ConditionExpression as race condition guard)
    # ADR #115: skip_origin is appended to the SET clause only when provided (skip_task
    # passes 'manual'); fail_task/mark_success never pass it, so their UpdateExpression
    # is byte-identical to before this change.
    skip_origin_set = ', skip_origin = :skip_origin' if skip_origin else ''
    skip_origin_values = {':skip_origin': skip_origin} if skip_origin else {}
    try:
        if include_error_field:
            executions_repo.update(
                execution_name,
                f'SET #s = :status, finished_at = :finished, #e = :error{skip_origin_set}',
                expr_values=expr_values_fn(
                    {
                        ':status': target_status,
                        ':finished': datetime.now(timezone.utc).isoformat(),
                        ':error': reason,
                        **skip_origin_values,
                    }
                ),
                expr_names={'#s': 'status', '#e': 'error'},
                condition_expr=condition_expr,
            )
        else:
            executions_repo.update(
                execution_name,
                f'SET #s = :status, finished_at = :finished{skip_origin_set}',
                expr_values=expr_values_fn(
                    {
                        ':status': target_status,
                        ':finished': datetime.now(timezone.utc).isoformat(),
                        **skip_origin_values,
                    }
                ),
                expr_names={'#s': 'status'},
                condition_expr=condition_expr,
            )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            log.warn(
                action_name, "Race condition: Task became terminal during operation", execution_name=execution_name
            )
            return cors_response(
                409,
                {'error': 'Task status changed during operation (race condition)', 'execution_name': execution_name},
            )
        else:
            raise

    # Side-effects AFTER successful claim
    stop_cause = reason or default_stop_cause or f'Task {action_name} via UI'
    stop_task_executions(item, stop_error, stop_cause)
    record_manual_decision(execution_name, action_name, stop_cause, item)
    _write_synthetic_output_marker(item, action_name, stop_cause, item.get('date', date))
    if emit_asset_events:
        _emit_asset_events_for_manual_success(item, actual_task_name, date)

    log.info(action_name, "Successfully completed", execution_name=execution_name, actual_task_name=actual_task_name)

    # Send orchestration callback
    if orchestration_token:
        try:
            callback_fn(orchestration_token, actual_task_name, reason)
        except (sfn.exceptions.TaskTimedOut, sfn.exceptions.TaskDoesNotExist):
            # Token expired or already consumed (e.g. mark_success/skip on waiting_decision task
            # where orchestration_token was already consumed by notify_dependents).
            # Expected — continue to notify_dependents so downstream unblocks.
            log.warn(
                action_name,
                "Orchestration token expired/consumed — continuing to notify dependents",
                execution_name=execution_name,
            )
        except sfn.exceptions.InvalidToken:
            # Invalid token format — continue
            log.warn(
                action_name,
                "Invalid orchestration token — continuing to notify dependents",
                execution_name=execution_name,
            )
        except Exception as e:
            err_str = str(e)
            log.error(action_name, "Failed orchestration callback", execution_name=execution_name, error=err_str)
            if 'AccessDenied' in err_str:
                detail = 'IAM permission missing: states:SendTaskSuccess. Run sam deploy to fix.'
            else:
                detail = err_str
            return cors_response(
                500,
                {
                    'error': 'Failed to send orchestration callback',
                    'detail': detail,
                    'execution_name': execution_name,
                },
            )
    else:
        # No token means task failed before Run_Task recorded it (e.g. IAM error at startup)
        # Still notify dependents so downstream tasks can unblock
        log.warn(
            action_name,
            "No orchestration token — task may have failed before starting",
            execution_name=execution_name,
            task=actual_task_name,
        )

    # Notify dependents via SFN helper
    if not notify_dependents_via_sfn(
        task_name=actual_task_name,
        status=target_status,
        date=item.get('date', date),
        pipeline_execution_short=pipeline_execution_short,
        pipeline_execution=pipeline_execution,
    ):
        log.warn(
            action_name,
            "Failed to notify dependents — downstream tasks may stay waiting",
            execution_name=execution_name,
        )
        _write_notify_warning(
            execution_name=execution_name,
            task_name=actual_task_name,
            pipeline_execution=pipeline_execution,
            pipeline_name=item.get('pipeline_name', 'unknown'),
            date=item.get('date', date),
            error='SFN start_execution failed',
        )

    resolve_pagerduty(item)
    return cors_response(
        200, {'message': success_message.format(execution_name=execution_name), 'execution_name': execution_name}
    )


def skip_task(task_name: str, event: Dict) -> Dict:
    """Skip a waiting/failed task.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    return _execute_task_action(
        task_name,
        event,
        action_name='skip',
        target_status='skipped',
        use_resolved_check=True,
        stop_error='Skipped',
        default_reason='Task skipped via UI',
        skip_origin='manual',
        callback_fn=lambda token, name, _reason: sfn.send_task_success(
            taskToken=token, output=json.dumps({'status': 'skipped', 'task_name': name})
        ),
        success_message='Skip triggered for {execution_name}',
    )


def fail_task(task_name: str, event: Dict) -> Dict:
    """Mark a running task as failed and stop its wrapper.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    return _execute_task_action(
        task_name,
        event,
        action_name='fail',
        target_status='failed',
        use_resolved_check=False,
        include_error_field=True,
        stop_error='ManuallyFailed',
        default_reason='Manually failed by user',
        callback_fn=lambda token, _name, reason: sfn.send_task_failure(
            taskToken=token, error='ManuallyFailed', cause=reason
        ),
        success_message='Task {execution_name} marked as failed',
    )


def mark_success(task_name: str, event: Dict) -> Dict:
    """Mark a task as successful manually.

    Supports both execution_name (full) and task_name (requires date lookup).

    Use cases:
    - Task stuck but work completed (verified via logs/S3)
    - Manual intervention - work done by human
    - Testing - quickly mark task as done
    """
    return _execute_task_action(
        task_name,
        event,
        action_name='mark_success',
        target_status='success',
        use_resolved_check=True,
        stop_error='ManuallySucceeded',
        default_reason='Manually marked successful by user',
        callback_fn=lambda token, name, reason: sfn.send_task_success(
            taskToken=token,
            output=json.dumps({'status': 'success', 'task_name': name, 'manual': True, 'reason': reason}),
        ),
        success_message='Task {execution_name} marked as successful',
        emit_asset_events=True,
    )


def stop_task(task_name: str, event: Dict) -> Dict:
    """Stop a running task (pause) without marking as failed. Task can be resumed later.
    Also handles stopping waiting tasks by marking them as aborted.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    body, err = safe_parse_body(event)
    if err:
        return err
    date = body.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    pipeline_execution = body.get('pipeline_execution', '')

    # Resolve task using unified resolver (handles both formats with pagination)
    item, execution_name = resolve_task_item(task_name, date, pipeline_execution)

    if not item:
        return cors_response(404, {'error': f'Task not found: {task_name} on {date}'})

    current_status = item.get('status', '')

    # Check if already terminal BEFORE doing anything
    if is_terminal_status(current_status):
        return cors_response(
            409,
            {
                'error': f'Task already in terminal state: {current_status}',
                'execution_name': execution_name,
                'current_status': current_status,
            },
        )

    actual_task_name = item.get('task_name', task_name)
    pipeline_execution = item.get('pipeline_execution', '')
    pipeline_execution_short = item.get('pipeline_execution_short', '')
    orchestration_token = item.get('orchestration_token')

    # Ensure pipeline_execution_short has a value (critical for notify_dependents)
    pipeline_execution_short = ensure_pipeline_execution_short(pipeline_execution, pipeline_execution_short)
    if not pipeline_execution_short:
        log.warn("stop_task", "No pipeline_execution_short, event notification may fail", execution_name=execution_name)

    # Determine final status based on current status
    # Running tasks become 'stopped', waiting tasks become 'aborted'
    if current_status in TASK_WAITING_STATUSES:
        final_status = TaskStatus.ABORTED
    else:
        final_status = TaskStatus.STOPPED

    # Claim: update status FIRST (only if not already terminal)
    try:
        executions_repo.update(
            execution_name,
            'SET #s = :status, finished_at = :finished',
            expr_values=build_condition_expression_values(
                {':status': final_status, ':finished': datetime.now(timezone.utc).isoformat()}
            ),
            expr_names={'#s': 'status'},
            condition_expr=TERMINAL_CONDITION_EXPRESSION,
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
            log.info(
                "stop_task", "Task already in terminal state, skipping status update", execution_name=execution_name
            )
            return cors_response(
                409,
                {
                    'error': 'Task already in terminal state',
                    'execution_name': execution_name,
                    'current_status': current_status,
                },
            )
        else:
            raise

    # Side-effects AFTER successful claim
    stop_task_executions(item, 'Stopped', 'Task stopped via UI - can be restarted')
    record_manual_decision(execution_name, 'stop', 'Task stopped via UI', item)
    _write_synthetic_output_marker(item, 'stop', 'Task stopped via UI', item.get('date', date))

    # For aborted tasks: send orchestration callback if token exists
    # This prevents pipeline from hanging waiting for callback
    if final_status == 'aborted' and orchestration_token:
        try:
            sfn.send_task_failure(taskToken=orchestration_token, error='TaskAborted', cause='Task aborted via UI')
        except Exception as e:
            log.error("stop_task", "Failed orchestration callback", execution_name=execution_name, error=str(e))

    # Notify dependents ONLY for aborted (terminal status)
    # stopped is NOT terminal - task can be restarted, so don't notify dependents yet
    if final_status == 'aborted':
        if not notify_dependents_via_sfn(
            task_name=actual_task_name,
            status=final_status,
            date=item.get('date', date),
            pipeline_execution_short=pipeline_execution_short,
            pipeline_execution=pipeline_execution,
        ):
            log.error("stop_task", "Failed to notify dependents", execution_name=execution_name)
            _write_notify_warning(
                execution_name=execution_name,
                task_name=actual_task_name,
                pipeline_execution=pipeline_execution,
                pipeline_name=item.get('pipeline_name', 'unknown'),
                date=item.get('date', date),
                error='SFN start_execution failed during stop',
            )

    return cors_response(
        200,
        {
            'message': f'Task {execution_name} {final_status}. Use Restart to resume.',
            'execution_name': execution_name,
            'status': final_status,
        },
    )


def restart_task(task_name: str, event: Dict) -> Dict:
    """Restart a task by calling the restart_task_helper Step Function.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    body, err = safe_parse_body(event)
    if err:
        return err
    date = body.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    pipeline_execution = body.get('pipeline_execution', '')

    # Resolve task using unified resolver (handles both formats with pagination)
    item, execution_name = resolve_task_item(task_name, date, pipeline_execution)

    if not item:
        return cors_response(404, {'error': f'Task not found: {task_name} on {date}'})

    current_status = item.get('status', '')

    # Restartable from any terminal status, 'stopped', OR 'waiting_decision' —
    # restart_task_helper's Stop_Old_Wrapper hard-kills whatever's still
    # running via states:StopExecution (not send_task_success/failure), so it
    # never triggers notify_dependents_via_sfn the way Stop does — going
    # straight to Restart from waiting_decision avoids Stop's unconditional,
    # immediate downstream notification entirely (see docs/reference/
    # SPIKE_TASK_ACTIONS_DATA_LIFECYCLE.md §8/§9 for the full reasoning).
    # Safe to include here specifically because both §9 prerequisites are in
    # place: (1) Stop_Old_Wrapper reads the correct field name (helper_arn
    # was a bug — the wrapper actually writes run_task_helper_arn — so it can
    # now actually find and kill the still-live wrapper, not silently no-op),
    # and (2) even if that kill fails for any reason, every status-writing
    # state in the wrapper now requires its own attempt to still match the
    # DB's current attempt — so a surviving ghost's write is rejected by
    # DynamoDB itself, and can never corrupt what the new attempt has
    # written, regardless of whether the kill succeeded.
    #
    # Restartable from any terminal status, OR from 'stopped' — stop_task's own
    # success message tells the user to "Use Restart to resume", so a stopped
    # task must be accepted here even though 'stopped' is deliberately NOT in
    # TASK_TERMINAL_STATUSES (stop_task treats it as non-terminal/resumable,
    # not as "done"). This is a restart_task-local extension, not a change to
    # the shared is_terminal_status/TASK_TERMINAL_STATUSES — stop_task's own
    # "already terminal" check and other actions still use the unmodified set.
    RESTARTABLE_STATUSES = TASK_TERMINAL_STATUSES | {'stopped', 'waiting_decision'}
    if current_status not in RESTARTABLE_STATUSES:
        return cors_response(
            409,
            {
                'error': f'Task not in restartable state: {current_status}. '
                         f'Only completed/failed/skipped/aborted/stopped/waiting_decision tasks can be restarted.',
                'execution_name': execution_name,
                'current_status': current_status,
            },
        )

    # RESTART_HELPER_ARN is set automatically by the SAM template (see
    # template.yaml — !Sub arn:aws:states:${AwsRegion}:${AWS::AccountId}:...).
    # Its absence means a broken/partial deploy; the previous fallback path was
    # dead code that couldn't do the two-level stop the helper does and would
    # leave ghost wrappers alive on waiting_decision restarts. Fail fast with a
    # clear error instead.
    restart_helper_arn = os.environ.get('RESTART_HELPER_ARN')
    if not restart_helper_arn:
        log.error(
            "restart_task",
            "RESTART_HELPER_ARN not configured — misconfigured deploy",
            execution_name=execution_name,
        )
        return cors_response(500, {'error': 'RESTART_HELPER_ARN not configured — check deploy'})

    # Record manual decision for timeline
    record_manual_decision(execution_name, 'restart', 'Task restarted via UI', item)

    # task_config/outlets are deploy-time DAG properties, never stored on
    # the per-execution record — look them up from the registry so the
    # restarted attempt doesn't silently lose retries/worker-type/etc or
    # outlets (previously always reconstructed as empty).
    node = _lookup_dag_node(item.get('pipeline_name', ''), item.get('task_name', ''))
    task_config = (node or {}).get('task_config', {})
    outlets = (node or {}).get('outlets', [])

    # Call restart_task_helper Step Function
    try:
        exec_id = uuid.uuid4().hex[:8]
        restart_name = f"restart-{execution_name[:60]}-{exec_id}"
        result = sfn.start_execution(
            stateMachineArn=restart_helper_arn,
            name=restart_name,
            input=json.dumps({
                'execution_name': execution_name,
                'task_config': task_config,
                'outlets': outlets,
            }),
        )
        return cors_response(
            200, {'message': f'Restart initiated for {execution_name}', 'execution_arn': result['executionArn']}
        )
    except Exception as e:
        log.error("unknown", "Unexpected error", error=str(e))
        return cors_response(500, {'error': f'Failed to start restart: {str(e)}'})

def register(router) -> None:
    """Register the free task read routes. See ADR #97."""
    router.add('GET', '/api/tasks', get_all_tasks)
    router.add('GET', '/api/task-config', get_task_config, 'name')
    router.add('GET', '/api/task-output', get_task_output, 'name')
    router.add('GET', '/api/task-events', get_task_events, 'name')
    # Task intervention — free (ADR #110): fix a stuck/failed task on a live run.
    router.add('POST', '/api/task-retry', retry_task, 'name')
    router.add('POST', '/api/task-skip', skip_task, 'name')
    router.add('POST', '/api/task-fail', fail_task, 'name')
    router.add('POST', '/api/task-success', mark_success, 'name')
    router.add('POST', '/api/task-stop', stop_task, 'name')
    router.add('POST', '/api/task-restart', restart_task, 'name')
