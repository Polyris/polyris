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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError, BotoCoreError

from config import sfn, ASSET_EVENT_TTL_DAYS
from polyris.coordination import actions, run_input, run_row, task_row
from polyris.coordination.records import warning_item
from dal import asset_events_repo, executions_repo, pipelines_repo, specs_repo
from dal.specs_repo import SpecsMissing
from polyris.coordination.task_spec import SpecError
from dal.task_events_repo import task_events_repo
from constants import Limits, TASK_SETTLED_STATUSES
from constants_generated import ManualResolution
from feed import feed_dates, is_older, page_by_started_at, pipeline_rows_before
from response import cors_response, safe_parse_body
from logger import log
from auth import operator_display
from utils import (
    should_skip_token_row,
    is_execution_name, safe_int, safe_param_int,
    resolve_pagerduty, retrieve_result, resolve_max_retries
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


# The feed reads date-pipeline-index or pipeline-date-index. Each projects only part of
# a row (sam/template.yaml), and DynamoDB rejects a query whose ProjectionExpression names
# an attribute the index does not project. So the index reads take no projection, and the
# fields that either index lacks are read from the base table for the returned page.
# retry_attempt (ADR #125) counts in-SFN auto-retries within one wrapper; `attempt`
# counts manual-restart generations across wrappers.
_TASKS_BACKFILL = ('running_at', 'dependencies', 'wait_for', 'wrapper_execution_arn',
                   *executions_repo.RUN_META_FIELDS)


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

    max_retries = resolve_max_retries(item)

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
        'notification_failed': item.get('notification_failed'),
        'triggered_by': item.get('triggered_by'),
        'attempt': safe_int(item.get('attempt')) if item.get('attempt') is not None else None,
        # retry_attempt (ADR #125) is the in-SFN auto-retry index within the current wrapper
        # — reset to 0 on every wrapper start, bumped by Save_Retry_Attempt on each retry.
        # `attempt > 1` = manual restart; `retry_attempt > 0` = auto-retry. Distinct axes.
        'retry_attempt': safe_int(item.get('retry_attempt'), 0),
        # max_retries is static config (auto-retries per wrapper run). Not displayed in the
        # list view (a static ceiling in a live feed is misleading). Included for the task
        # detail panel to surface alongside attempt.
        'max_retries': max_retries,
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
            )
        elif pipeline_filter:
            # One pipeline, no date: pipeline-date-index, no window (ADR #108) — the
            # same read /api/runs uses, so both halves of History reach equally far
            # back. Replaces a full-table Scan: unbounded cost, and its arbitrary row
            # order made "the newest N" a lottery no cursor could page honestly.
            items = pipeline_rows_before(
                pipeline_filter, before, limit,
                count_fn=lambda rows: sum(1 for r in rows if _keeps(r)),
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
                    ))
                except Exception as e:
                    log.error("get_all_tasks", "Error querying date", error=str(e), date_str=date_str)

        # Skip internal/special records (_pause_, _notify_warn_, output#) and Backfill
        # records, and pipeline-level/partial rows with no task_name — before paging,
        # so a page is a pageful of real rows.
        rows = [i for i in items if not should_skip_token_row(i) and i.get('task_name')]

        page, next_cursor = page_by_started_at(rows, before, limit)

        # Fields the index read lacks (_TASKS_BACKFILL), from the base table for the
        # current page only (bounded cost: ≤ limit rows = ≤ 5 BatchGetItem calls).
        try:
            names = [r['execution_name'] for r in page if r.get('execution_name')]
            if names:
                meta = executions_repo.batch_get_run_meta(names, _TASKS_BACKFILL)
                for r in page:
                    m = meta.get(r.get('execution_name', ''))
                    if m:
                        for field in _TASKS_BACKFILL:
                            if r.get(field) is None:
                                r[field] = m.get(field)
        except (ClientError, BotoCoreError) as e:
            log.warn("get_all_tasks", "batch_get_run_meta failed; run-meta fields left null",
                     error=str(e))

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

    Returns config fields plus live retry_attempt (ADR #125).
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
            'max_retries': resolve_max_retries(item)
        },
        # Runtime state (read-only, for reference)
        'runtime': {
            # retry_attempt (ADR #125) replaces the dead legacy `retry_attempts` field —
            # no SFN state ever wrote the plural, singular is what Save_Retry_Attempt
            # writes on every in-SFN retry.
            'retry_attempt': safe_int(item.get('retry_attempt'), 0),
            'status': item.get('status', 'unknown')
        }
    })


def get_task_output(task_name: str, event: Dict) -> Dict:
    """Return a task's stored input and output.

    Reads two DDB rows keyed by pipeline + task_name + date:

    * ``output#{pipeline}#{task}#{date}`` — carries the ``result`` field
      (what the task returned or pushed via ``xcom.push()``). Large results
      offloaded to S3 (``_s3_ref``) are resolved transparently;
      ``truncated: true`` means the stored result exceeded the inline limit.
    * ``input#{pipeline}#{task}#{date}`` — new in 1.0.0: carries the
      ``task_input`` blob (upstream + variables) up to ~380 KB. Split out
      from the ``output#`` row so the Console preview is no longer bounded
      by the shared 25 KB truncation cap the old design imposed.

    Falls back to reading ``task_input`` off the ``output#`` row for
    pre-1.0.0 pipelines that haven't produced a new-shape run yet.
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
    # `run_id` on canonical rows (set by Init_Output_Row / Save_Canonical_Output /
    # Save_Input_Record — all use $states.context.Execution.Id = run_task_helper
    # ARN). Returned so the UI can detect cross-run staleness: rows keyed by
    # (pipeline, task, DATE) are shared across every same-date run; a row whose
    # run_id doesn't match THIS task's run_task_helper_arn was written by a
    # prior same-date run and its content should not be attributed to this run.
    # See CLAUDE.md rule #30 (extended in the same delivery to catch settled
    # tasks with stale-row bleed).
    output_row_run_id = None
    input_row_run_id = None
    if pipeline_name:
        output_key = f"output#{pipeline_name}#{plain_task}#{run_date}"
        input_key = f"input#{pipeline_name}#{plain_task}#{run_date}"
        try:
            # Result lives on the output# row.
            store_item = executions_repo.get(output_key) or {}
            output_row_run_id = store_item.get('run_id')
            raw = store_item.get('result')
            if raw:
                try:
                    parsed = json.loads(raw)
                except ValueError:
                    parsed = raw    # a string result a 1.2 run_task stored as it was, not as JSON
                if isinstance(parsed, dict) and parsed.get('_truncated'):
                    truncated = True
                else:
                    output = retrieve_result(parsed)

            # task_input: prefer the new input# record; fall back to the
            # legacy field on output# for pre-1.0.0 pipelines. Isolated
            # try so a missing/failing input# lookup can't hide the output.
            raw_input = None
            try:
                input_item = executions_repo.get(input_key) or {}
                input_row_run_id = input_item.get('run_id')
                raw_input = input_item.get('task_input')
            except (ClientError, BotoCoreError) as inner:
                log.error("get_task_output", "Error reading input# record; falling back to legacy field",
                          error=str(inner), task_name=task_name)
            if not raw_input:
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
        # Canonical-row run_ids for the UI's cross-run staleness gate.
        # `expected_run_id` is the run_task_helper ARN this run's wrapper
        # invoked (set on the per-run task row by Send_Ready_Signal_Sfn); UI
        # compares row.run_id vs this to know if the row belongs to this run.
        'output_row_run_id': output_row_run_id,
        'input_row_run_id': input_row_run_id,
        'expected_run_id': resolved.get('run_task_helper_arn'),
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
            'attempt': safe_int(evt.get('attempt'), 1),
            # retry_attempt (ADR #125) — present on TASK_STARTED (always 0 for first
            # dispatch), TASK_RETRY_STARTED (1..retries), and both TASK_FINISHED
            # variants. Older events pre-ADR default to 0 via safe_int.
            'retry_attempt': safe_int(evt.get('retry_attempt'), 0),
        })
    
    return cors_response(200, {
        'execution_name': execution_name,
        'task_run_id': task_run_id,
        'events': formatted_events,
        'event_count': len(formatted_events)
    })


def retry_task(task_name: str, event: Dict) -> Dict:
    """Retry a failed/skipped task.

    DEPRECATED: This endpoint is deprecated. Use restart_task instead.
    This function now simply delegates to restart_task for backward compatibility.
    """
    # Delegate to restart_task for actual functionality
    return restart_task(task_name, event)


def _emit_marker_blocked_warn(
    pipeline_name: str, task_name: str, date: str,
    action_name: str, operator: str,
) -> None:
    """Emit a `_notify_warn_*` record (Principle #38) when
    `_write_synthetic_output_marker`'s guard blocks a manual action from
    landing its marker because real output is already present. Best-effort —
    a failure here must not block the manual action either."""
    try:
        # A `manual` warning: the bell reads it by write time, so it shows even
        # when the operator acted on an old partition (ADR #130 §9).
        executions_repo.put(warning_item(
            kind="manual", execution_name=f"marker_blocked_{pipeline_name}_{task_name}_{date}_{action_name}",
            task_name=task_name, pipeline_name=pipeline_name, pipeline_execution="", task_date=date,
            error=(
                f"Manual '{action_name}' by {operator} did not write a marker — "
                f"real output for {task_name}@{date} already existed on the canonical row "
                "and was preserved. Downstream reads see that real output, not the manual "
                "resolution."
            ),
            now=datetime.now(timezone.utc),
        ))
    except (ClientError, BotoCoreError) as e:
        log.warn(
            "_emit_marker_blocked_warn",
            "Failed to emit marker-blocked notification",
            error=str(e), task_name=task_name,
        )


def _run_task_spec(run_item: Dict, task_name: str) -> Tuple[Optional[Dict], str]:
    """A task's spec fields from its run's spec version (ADR #137 §8), or why they cannot be read. Never the
    registry's version: a redeploy since the run started may have changed the task's outlets, or removed it.

    The spec holds the outlets and task_config (``TaskSpecKey``); wrapper steps have one too.
    """
    spec_id = run_item.get(run_row.SPEC_ID)
    if not spec_id:
        return None, 'its run row names no spec version'
    try:
        version = specs_repo.version(str(spec_id), int(run_item.get(run_row.TASK_COUNT, 0)))
    except (SpecsMissing, SpecError, ClientError, BotoCoreError, ValueError) as e:
        return None, f"its run's spec version cannot be read: {e}"
    spec = version.get(task_name)
    if spec is None:
        return None, f"task {task_name} is not in its run's spec version"
    return dict(spec.fields), ''


#: An action whose transaction a row's move cancelled is built again from new reads, this many times in all.
ACTION_ATTEMPTS = 3


def _execute_task_action(task_name: str, event: Dict, *, action: str, default_reason: str,
                         success_message: str) -> Dict:
    """Skip, Fail, Mark Successful and Stop from the request's body, through ``apply_task_action``."""
    body, err = safe_parse_body(event)
    if err:
        return err
    date = body.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    item, execution_name = resolve_task_item(task_name, date, body.get('pipeline_execution', ''))
    if not item:
        log.warn(action, "Task not found", task_name=task_name, date=date)
        return cors_response(404, {'error': f'Task not found: {task_name} on {date}'})
    return apply_task_action(action, item, execution_name, reason=body.get('reason') or default_reason,
                             operator=operator_display(event), success_message=success_message)


def apply_task_action(action: str, item: Dict, execution_name: str, *, reason: str, operator: str,
                      success_message: str) -> Dict:
    """An operator's Skip, Fail, Mark Successful or Stop of a task (ADR #137 §3).

    One transaction that ``coordination.actions`` builds from this function's reads: the row, its run row, the
    date's ``output#``, and for Mark Successful the task's outlets. Then, best effort, the timeline events, the
    stop of the task's run_task, and a ``manual`` warning when real output kept the marker out. The dispatcher
    sees the move through the tasks table's stream: nothing here starts, answers or notifies.
    """
    pipeline = str(item.get('pipeline_name', ''))
    task_name = str(item.get('task_name', ''))
    run_execution = str(item.get('pipeline_execution', ''))

    def build(task: Dict, run_item: Dict) -> 'actions.ActionWrite':
        date = str(run_item.get(run_row.RUN_DATE) or task.get('date', ''))
        outlets: List[Dict] = []
        if action == ManualResolution.MARK_SUCCESS:
            node, problem = _run_task_spec(run_item, task_name)
            if problem:
                # Its facts are part of the success (invariant 12): without its outlets it cannot be recorded.
                raise actions.ActionRefused(f"{execution_name}: its outlets cannot be read ({problem}); "
                                            "Mark Successful would record none of its facts")
            outlets = list((node or {}).get('outlets') or [])
        stored = executions_repo.get(run_row.input_key(run_execution))
        return actions.task_action(
            action=action, task=task, run=run_item,
            output=executions_repo.get(f'output#{pipeline}#{task_name}#{date}', consistent=True),
            outlets=outlets, cascade=run_input.from_item(stored).cascade if stored else 'auto',
            operator=operator, reason=reason, now=datetime.now(timezone.utc), tokens_table=executions_repo.table.name,
            asset_events_table=asset_events_repo.table.name, fact_ttl_days=ASSET_EVENT_TTL_DAYS)

    write, item, refusal = _transact(action, execution_name, item, build)
    if refusal is not None:
        return refusal
    _after_action(action, item, write, pipeline=pipeline, task_name=task_name, date=str(item.get('date', '')),
                  operator=operator)
    log.info(action, "Done", execution_name=execution_name)
    return cors_response(200, {'message': success_message.format(execution_name=execution_name),
                               'execution_name': execution_name})


def _transact(action: str, execution_name: str, item: Dict,
              build: Callable[[Dict, Dict], Any]) -> Tuple[Any, Dict, Optional[Dict]]:
    """Read the run row, build the action's transaction from the reads and send it (ADR #137 §3). A transaction a
    row's move cancelled (a pass, run_task, a push) is built again from new reads while the action still applies.

    Returns what ``build`` made and the row as last read, or the answer that refuses the action.
    """
    run_execution = str(item.get('pipeline_execution', ''))
    for _ in range(ACTION_ATTEMPTS):
        run_item = executions_repo.get(run_row.key(run_execution), consistent=True)
        if run_item is None:
            return None, item, cors_response(409, {
                'error': f'{execution_name}: a task of a run from before 1.3 takes no action; '
                         'run its pipeline again after the redeploy', 'execution_name': execution_name})
        try:
            write = build(item, run_item)
        except actions.ActionRefused as exc:
            return None, item, cors_response(409, {'error': str(exc), 'execution_name': execution_name,
                                                   'current_status': item.get('status')})
        try:
            executions_repo.transact_write(write.items)
            return write, item, None
        except executions_repo.transaction_canceled_exception as exc:
            log.info(action, "The transaction was cancelled: built again from new reads", execution_name=execution_name,
                     reasons=[str(r.get('Code', '')) for r in exc.response.get('CancellationReasons', [])])
            fresh = executions_repo.get(execution_name, consistent=True)
            if fresh is None:
                return None, item, cors_response(404, {'error': f'Task not found: {execution_name}'})
            item = fresh
    return None, item, cors_response(409, {'error': f'{execution_name} kept changing under the action; try again',
                                           'execution_name': execution_name})


def _after_action(action: str, item: Dict, write: 'actions.ActionWrite', *, pipeline: str, task_name: str,
                  date: str, operator: str) -> None:
    """What follows an action's transaction, best effort: the row holds the state (ADR #137 §3)."""
    for event in write.events:
        try:
            task_events_repo.put(event)
        except (ClientError, BotoCoreError) as e:
            log.error(action, "Could not write the timeline event", event=event.get('event_type'), error=str(e))
    arn = item.get(task_row.RUN_TASK_ARN)
    if write.run_task_to_stop and arn:
        try:
            sfn.stop_execution(executionArn=arn, error='Console', cause=f'stopped by {operator}')
        except (ClientError, BotoCoreError) as e:
            # The fence keeps whatever it does next out of the row: its writes are conditional on its generation.
            log.error(action, "Could not stop the task's run_task", arn=arn, error=str(e))
    if write.marker_blocked:
        _emit_marker_blocked_warn(pipeline, task_name, date, action, operator)
    resolve_pagerduty(item)


def skip_task(task_name: str, event: Dict) -> Dict:
    """Skip a waiting/failed task.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    return _execute_task_action(task_name, event, action=ManualResolution.SKIP, default_reason='Task skipped via UI',
                                success_message='Skip triggered for {execution_name}')


def fail_task(task_name: str, event: Dict) -> Dict:
    """Mark a task as failed; a running one's run_task is stopped as cleanup (ADR #137 §3).

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    return _execute_task_action(task_name, event, action=ManualResolution.FAIL, default_reason='Manually failed by user',
                                success_message='Task {execution_name} marked as failed')


def mark_success(task_name: str, event: Dict) -> Dict:
    """Mark a task as successful manually.

    Supports both execution_name (full) and task_name (requires date lookup).

    Use cases:
    - Task stuck but work completed (verified via logs/S3)
    - Manual intervention - work done by human
    - Testing - quickly mark task as done
    """
    return _execute_task_action(task_name, event, action=ManualResolution.MARK_SUCCESS, default_reason='Manually marked successful by user',
                                success_message='Task {execution_name} marked as successful')


def stop_task(task_name: str, event: Dict) -> Dict:
    """Stop a task that has not finished: ``stopped``, final for its dependants (ADR #138); Restart runs it again.

    Supports both execution_name (full) and task_name (requires date lookup).
    """
    return _execute_task_action(task_name, event, action=ManualResolution.STOP, default_reason='Task stopped via UI',
                                success_message='Task {execution_name} stopped. Use Restart to run it again.')


def restart_task(task_name: str, event: Dict) -> Dict:
    """Restart a task, and with ``with_dependants`` in the body the dependants a failure or a stop decided
    (ADR #137 §3).

    One transaction that ``coordination.actions.restart`` builds: the row back to ``waiting`` and its run row's
    request, which the dispatcher carries out in passes (the renewal of the run's rows, the reset of the
    dependants). Supports both execution_name (full) and task_name (requires date lookup).
    """
    body, err = safe_parse_body(event)
    if err:
        return err
    date = body.get('date', datetime.now(timezone.utc).strftime('%Y-%m-%d'))
    item, execution_name = resolve_task_item(task_name, date, body.get('pipeline_execution', ''))
    if not item:
        return cors_response(404, {'error': f'Task not found: {task_name} on {date}'})
    return apply_restart(item, execution_name, with_dependants=body.get('with_dependants') is True,
                         operator=operator_display(event), reason=body.get('reason') or 'Task restarted via UI')


def apply_restart(item: Dict, execution_name: str, *, with_dependants: bool, operator: str, reason: str) -> Dict:
    """A Restart of the task row ``item`` (ADR #137 §3), as the console and the paid overlay's Slack button send it:
    ``coordination.actions.restart``'s transaction through ``_transact``, then its timeline event."""

    def build(task: Dict, run_item: Dict) -> 'actions.RestartWrite':
        return actions.restart(task=task, run=run_item, with_dependants=with_dependants, operator=operator,
                               reason=reason, now=datetime.now(timezone.utc), tokens_table=executions_repo.table.name)

    write, item, refusal = _transact('restart', execution_name, item, build)
    if refusal is not None:
        return refusal
    for timeline_event in write.events:
        try:
            task_events_repo.put(timeline_event)
        except (ClientError, BotoCoreError) as e:
            log.error('restart', "Could not write the timeline event", execution_name=execution_name, error=str(e))
    log.info('restart', "Done", execution_name=execution_name, with_dependants=with_dependants)
    message = f'Task {execution_name} restarted' + (' with its dependants' if with_dependants else '')
    return cors_response(200, {'message': message, 'execution_name': execution_name})


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
