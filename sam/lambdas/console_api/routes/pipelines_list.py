"""Pipeline listing and status routes.

Handles:
- list_pipelines: List all pipelines with SLA and progress
- get_pipeline_status: Get detailed status for a specific pipeline
- get_pipeline_executions: Get list of pipeline executions

Helper functions:
- _aggregate_executions: Group and aggregate execution data
- _query_pipeline_by_date_range: Query tokens table by date range
- _reconcile_running: Reconcile running status with SFN state
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Dict

from boto3.dynamodb.conditions import Key, Attr
from botocore.exceptions import ClientError, BotoCoreError

from config import sfn
from dal import executions_repo, pipelines_repo
from constants import Limits, normalize_execution_status, derive_execution_status, reconcile_execution_status, TASK_SETTLED_STATUSES, TASK_SUCCESS_STATUSES
from feed import feed_dates
from response import cors_response
from logger import log
from utils import safe_int, safe_param_int, retrieve_result, parse_wait_before, should_skip_token_row

def list_pipelines(event: Dict) -> Dict:
    """
    List pipelines with optional stats (SLA, progress).
    
    Optimized:
    - Uses pipeline_registry as source of truth (small table)
    - Stats scan limited to Limits.MAX_STATS_ITEMS and last SLA_DAYS
    - Stats are optional via ?stats=true parameter
    """
    params = event.get('queryStringParameters', {}) or {}
    include_stats = params.get('stats', 'false').lower() == 'true'
    
    pipelines = {}
    today = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')

    # 1. Get pipelines from registry (authoritative source, small table)
    try:
        registry_items = pipelines_repo.list_all(max_items=Limits.MAX_SCAN_ITEMS)
        
        for item in registry_items:
            name = item.get('pipeline_name', '')
            if name:
                # asset_schedule (JSON string, {'operator': 'AND'|'OR', 'assets': [...]})
                # is set for asset-triggered pipelines instead of a cron 'schedule' —
                # without surfacing it, the UI can't tell "genuinely manual" apart
                # from "asset-triggered" (both have an empty schedule string).
                asset_schedule = None
                raw_asset_schedule = item.get('asset_schedule', '')
                if raw_asset_schedule:
                    try:
                        parsed = json.loads(raw_asset_schedule)
                        if parsed and parsed.get('assets'):
                            asset_schedule = parsed
                    except (json.JSONDecodeError, TypeError):
                        log.warn("list_pipelines", "Malformed asset_schedule in registry",
                                 pipeline_name=name, raw=raw_asset_schedule)
                pipelines[name] = {
                    'name': name,
                    'arn': item.get('sfn_arn', ''),
                    'description': item.get('description', ''),
                    'group': item.get('pipeline_group', ''),
                    'schedule': item.get('schedule', ''),
                    'asset_schedule': asset_schedule,
                    'status': 'idle',
                    'paused': False,
                    'sla': None,
                    'progress': None,
                    'today_stats': None,
                    'recent_runs': None
                }
    except (ClientError, BotoCoreError) as e:
        log.error("list_pipelines", "Error scanning registry", error=str(e))
    
    # If no pipelines in registry, fall back to tokens table scan (limited)
    if not pipelines:
        try:
            scan_kwargs = {
                'ProjectionExpression': 'pipeline_name',
                'Limit': 1000  # Reasonable limit for discovery
            }
            response = executions_repo.scan_raw(**scan_kwargs)
            for item in response.get('Items', []):
                name = item.get('pipeline_name', '')
                if name and name != 'unknown' and name not in pipelines:
                    pipelines[name] = {
                        'name': name,
                        'arn': '',
                        'description': '',
                        'status': 'idle',
                        'paused': False,
                        'sla': None,
                        'progress': None,
                        'today_stats': None
                    }
        except (ClientError, BotoCoreError) as e:
            log.error("list_pipelines", "Error scanning tokens for discovery", error=str(e))
    
    # 2. Calculate stats only if requested
    # Uses date-pipeline-index GSI (hash=date) instead of full table scan.
    # 14 targeted queries (1 per SLA day) vs scanning entire table.
    if include_stats and pipelines:
        pipeline_runs = {}  # {pipeline_name: {execution_id: {statuses: set(), date: str}}}
        pipeline_today = {}  # {pipeline_name: {success: 0, failed: 0, running: 0, waiting: 0, total: 0}}
        
        try:
            # Same SLA window every History feed walks (feed.py) — no cursor here since
            # this is a fixed 14-day rollup for the sidebar, not a paginated feed, but
            # the sequence of dates is the exact one `feed_dates('')` already produces.
            sla_dates = feed_dates('')
            
            for query_date in sla_dates:
                try:
                    items = executions_repo.query_runs_by_date(
                        query_date,
                        min_rows=Limits.MAX_STATS_ITEMS,
                        projection='pipeline_name, pipeline_execution, #s',
                        expr_names={'#s': 'status'}
                    )
                except (ClientError, BotoCoreError) as e:
                    log.error("list_pipelines", "Error querying date", error=str(e), query_date=query_date)
                    continue
                
                for item in items:
                    # Skip internal/special records (_pause_, _notify_warn_, etc.)
                    if should_skip_token_row(item):
                        continue
                    name = item.get('pipeline_name', '')
                    if not name or name == 'unknown' or name not in pipelines:
                        continue
                    
                    exec_id = item.get('pipeline_execution', '')
                    task_status = item.get('status', '')
                    
                    # Collect data for SLA (all dates in range)
                    if exec_id:
                        if name not in pipeline_runs:
                            pipeline_runs[name] = {}
                        if exec_id not in pipeline_runs[name]:
                            pipeline_runs[name][exec_id] = {'statuses': set(), 'date': query_date}
                        pipeline_runs[name][exec_id]['statuses'].add(task_status)
                    
                    # Collect today's task stats for progress
                    if query_date == today:
                        if name not in pipeline_today:
                            pipeline_today[name] = {'success': 0, 'failed': 0, 'running': 0, 'waiting': 0, 'skipped': 0, 'total': 0}
                        pipeline_today[name]['total'] += 1
                        if task_status in ('success', 'succeeded'):
                            pipeline_today[name]['success'] += 1
                        elif task_status in ('failed', 'upstream_failed', 'aborted', 'stopped'):
                            pipeline_today[name]['failed'] += 1
                        elif task_status in ('running', 'pending', 'deps_ready', 'waiting_delay'):
                            pipeline_today[name]['running'] += 1
                        elif task_status == 'skipped':
                            pipeline_today[name]['skipped'] += 1
                        else:
                            pipeline_today[name]['waiting'] += 1
                
        except (ClientError, BotoCoreError, KeyError, TypeError) as e:
            log.error("list_pipelines", "Error calculating stats", error=str(e))
        
        # Calculate SLA and progress for each pipeline
        for name in pipelines:
            # SLA: % of COMPLETED runs that succeeded (no failed tasks)
            # In-progress runs are excluded from SLA calculation
            if name in pipeline_runs and pipeline_runs[name]:
                runs = pipeline_runs[name]
                # SLA: % of completed runs that succeeded. In-progress runs are
                # excluded; aborted (user-stopped) runs are excluded too — a stop is
                # not a system failure (ADR #112). Status is the canonical derivation,
                # shared with /runs and the execution dropdown.
                succeeded_runs = 0
                total_completed_runs = 0
                run_results = []
                for exec_id, data in runs.items():
                    run_status = derive_execution_status(data['statuses'])
                    run_results.append({
                        'date': data.get('date', ''),
                        'exec': exec_id[-8:] if len(exec_id) > 8 else exec_id,
                        'status': run_status,
                    })
                    if run_status in ('running', 'aborted'):
                        continue
                    total_completed_runs += 1
                    if run_status == 'success':
                        succeeded_runs += 1
                if total_completed_runs > 0:
                    pipelines[name]['sla'] = round(succeeded_runs / total_completed_runs * 100)

                # Recent runs (newest first): sparkline + the card's status.
                run_results.sort(key=lambda r: r['date'], reverse=True)
                pipelines[name]['recent_runs'] = run_results[:10]
                # Card status reflects the LAST run's canonical status (ADR #112) — the
                # "is this pipeline healthy?" signal, consistent with the sparkline. The
                # 'idle' default remains when there are no runs in the recent window.
                if run_results:
                    pipelines[name]['status'] = run_results[0]['status']

            # Today's task stats — for the progress bar only. (The card status is the
            # last run's status, set above, not a per-day aggregate.)
            if name in pipeline_today:
                stats = pipeline_today[name]
                pipelines[name]['today_stats'] = stats
                if stats['total'] > 0:
                    # Progress = terminal tasks / total tasks
                    completed = stats['success'] + stats['failed'] + stats['skipped']
                    pipelines[name]['progress'] = round(completed / stats['total'] * 100)
    
    # 3. Get ARNs from Step Functions API (only if not already from registry)
    pipelines_without_arn = [name for name, data in pipelines.items() if not data.get('arn')]
    if pipelines_without_arn:
        try:
            paginator = sfn.get_paginator('list_state_machines')
            for page in paginator.paginate():
                for sm in page.get('stateMachines', []):
                    sm_name = sm['name']
                    # Match by name (exact or partial)
                    if sm_name in pipelines and not pipelines[sm_name]['arn']:
                        pipelines[sm_name]['arn'] = sm['stateMachineArn']
                    else:
                        # Try partial match (pipeline name might be part of SFN name)
                        for p_name in pipelines_without_arn:
                            if (p_name in sm_name or sm_name in p_name) and not pipelines[p_name]['arn']:
                                pipelines[p_name]['arn'] = sm['stateMachineArn']
        except (ClientError, BotoCoreError, KeyError) as e:
            log.error("list_pipelines", "Error listing state machines", error=str(e))
    
    return cors_response(200, {
        'pipelines': list(pipelines.values())
    })


def get_pipeline_status(pipeline_name: str, event: Dict) -> Dict:
    """Get detailed status for a specific pipeline."""
    # Get filters from query params
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    pipeline_execution = params.get('pipeline_execution', '')
    
    # Query by pipeline_execution (GSI) if provided, otherwise scan by date
    if pipeline_execution:
        # Use GSI for efficient query - returns tasks for specific execution only
        items = executions_repo.query_by_pipeline_execution(pipeline_execution)
    else:
        # Fallback to scan with date filter (with pagination)
        items = executions_repo.scan(
            max_items=Limits.MAX_FETCH_ITEMS,
            FilterExpression=Attr('pipeline_name').eq(pipeline_name) & Attr('date').eq(date)
        )
    
    # Group tasks by execution, deduplicating tasks within each execution
    execution_tasks = {}  # {pipeline_execution: {tasks: {task_name: item}, started_at: str}}
    task_map = {}  # Final task map to return
    
    for item in items:
        # Skip internal/special records (_pause_, _notify_warn_, etc.)
        if should_skip_token_row(item):
            continue
        exec_name = item.get('execution_name', '')
        # Use task_name field directly - don't derive from execution_name
        task_name = item.get('task_name', '')
        if not task_name:
            # Fallback: extract task name from execution_name
            task_name = exec_name.rsplit('-', 2)[0] if exec_name.count('-') >= 2 else exec_name
        
        # Group by pipeline_execution
        pe = item.get('pipeline_execution', 'unknown')
        if pe not in execution_tasks:
            execution_tasks[pe] = {'tasks': {}, 'started_at': item.get('started_at', '')}
        
        # Update earliest started_at for this execution
        started_at = item.get('started_at', '')
        if started_at and (not execution_tasks[pe]['started_at'] or started_at < execution_tasks[pe]['started_at']):
            execution_tasks[pe]['started_at'] = started_at
        
        execution_tasks[pe]['tasks'][task_name] = item
    
    # If pipeline_execution was specified, use that; otherwise pick the latest by started_at
    selected_execution = None
    if pipeline_execution:
        # Use tasks from specified execution
        if pipeline_execution in execution_tasks:
            task_map = {k: v for k, v in execution_tasks[pipeline_execution]['tasks'].items()}
            selected_execution = pipeline_execution
    else:
        # Pick the latest execution by started_at
        best_exec = None
        for pe, data in execution_tasks.items():
            if not best_exec or data['started_at'] > execution_tasks[best_exec]['started_at']:
                best_exec = pe
        
        if best_exec:
            task_map = {k: v for k, v in execution_tasks[best_exec]['tasks'].items()}
            selected_execution = best_exec
    
    # Now build the final task list
    tasks = []
    for task_name, item in task_map.items():
        # Parse dependencies
        deps_str = item.get('dependencies', '[]')
        try:
            deps = json.loads(deps_str) if isinstance(deps_str, str) else deps_str
        except (json.JSONDecodeError, TypeError, ValueError):
            deps = []
        
        # Parse result
        result_str = item.get('result', '{}')
        try:
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
            # Handle Claim Check - retrieve from S3 if it's a reference
            result = retrieve_result(result)
        except (json.JSONDecodeError, TypeError, ValueError):
            result = {}
        
        tasks.append({
            'execution_name': item.get('execution_name', ''),
            'task_name': task_name,
            'status': item.get('status', 'unknown'),
            'dependencies': deps,
            'wait_for': item.get('wait_for', '[]'),
            'started_at': item.get('started_at', ''),
            'running_at': item.get('running_at'),
            'finished_at': item.get('finished_at'),
            'error': item.get('error'),
            'result': result,
            'wait_before': parse_wait_before(item.get('wait_before')),
            'wait_delay_started_ms': safe_int(item.get('wait_delay_started_ms')),
            'wait_delay_until_ms': safe_int(item.get('wait_delay_until_ms')),
            'trigger_rule': item.get('trigger_rule', 'all_success'),
            'config': {
            },
            'pipeline_name': item.get('pipeline_name', 'unknown'),
            'pipeline_execution': item.get('pipeline_execution'),
            'pipeline_execution_short': item.get('pipeline_execution_short'),
            'task_execution_arn': item.get('task_execution_arn'),
            'wrapper_execution_arn': item.get('wrapper_execution_arn'),
            'notification_failed': item.get('notification_failed'),
            'task_type': item.get('task_type', 'sfn'),
            'date': item.get('date', date),
        })
    
    # Reconcile: if the execution is failed/aborted, mark not-yet-settled tasks as
    # 'aborted' — but skip tasks whose wrapper is still running (e.g. restarted tasks).
    # "Settled" = terminal or deliberately stopped (TASK_SETTLED_STATUSES, single source).
    if selected_execution:
        try:
            reg_item = pipelines_repo.get(pipeline_name)
            sm_arn = (reg_item or {}).get('sfn_arn', '') or (reg_item or {}).get('arn', '')
            if sm_arn:
                exec_arn = sm_arn.replace(':stateMachine:', ':execution:') + ':' + selected_execution
                exec_resp = sfn.describe_execution(executionArn=exec_arn)
                exec_status = exec_resp.get('status', '')
                if exec_status in ('FAILED', 'TIMED_OUT', 'ABORTED'):
                    reconciled = []
                    for t in tasks:
                        if t['status'] not in TASK_SETTLED_STATUSES:
                            # Check if wrapper is still running before marking aborted
                            w_arn = t.get('wrapper_execution_arn', '')
                            if w_arn:
                                try:
                                    w_resp = sfn.describe_execution(executionArn=w_arn)
                                    if w_resp.get('status', '') == 'RUNNING':
                                        reconciled.append(t)  # Wrapper alive, not orphaned
                                        continue
                                except (ClientError, BotoCoreError):
                                    pass  # Can't verify wrapper — mark aborted below
                            reconciled.append({**t, 'status': 'aborted'})
                        else:
                            reconciled.append(t)
                    tasks = reconciled
        except (ClientError, BotoCoreError) as e:
            log.error("get_pipeline_status", "Reconciliation check failed", error=str(e))
    
    # Calculate stats
    stats = {
        'total': len(tasks),
        'success': len([t for t in tasks if t['status'] in ['success', 'succeeded']]),
        'running': len([t for t in tasks if t['status'] in ['running', 'deps_ready']]),
        'waiting': len([t for t in tasks if t['status'] in ['waiting', 'waiting_delay', 'waiting_paused', 'waiting_decision']]),
        'failed': len([t for t in tasks if t['status'] in ['failed', 'upstream_failed']]),
        'skipped': len([t for t in tasks if t['status'] == 'skipped']),
        'stopped': len([t for t in tasks if t['status'] in ['stopped', 'aborted']]),
    }
    
    # The pipeline these tasks actually belong to — not the one that was asked for.
    # `?pipeline_execution=` looks up by execution id alone, so it can hand back another
    # pipeline's rows (the console clears its selected execution when you switch pipeline,
    # but that is the caller's discipline, not this route's guarantee). Echoing the
    # requested name made the response agree with the question no matter what it returned,
    # which is exactly what the console's stale-response guard checks — so the guard could
    # never fire. Say what the rows say; fall back to the request only when there are none.
    actual_pipeline = next(
        (i.get('pipeline_name') for i in items
         if not should_skip_token_row(i) and i.get('pipeline_name')),
        pipeline_name,
    )

    return cors_response(200, {
        'pipeline_name': actual_pipeline,
        'date': date,
        'tasks': tasks,
        'stats': stats,
        'selected_execution': selected_execution,  # Which execution the tasks are from
        'server_now_ms': int(datetime.now(timezone.utc).timestamp() * 1000)
    })

def _aggregate_executions(items):
    """Group task items by pipeline_execution and aggregate status.
    
    DATA SOURCE: DynamoDB (see DESIGN_DECISIONS.md #22).
    DynamoDB is the system of record for all UI reads.
    Status is derived from task statuses via the canonical helper (ADR #112).
    """
    exec_map = {}
    for item in items:
        # Skip internal/special records (_pause_, _notify_warn_, etc.)
        if should_skip_token_row(item):
            continue
        pe = item.get('pipeline_execution')
        if not pe or pe == 'unknown':
            continue
        
        if pe not in exec_map:
            exec_map[pe] = {
                'execution_id': pe,
                'execution_short': item.get('pipeline_execution_short', pe[:20] if pe else ''),
                'date': item.get('date'),
                'started_at': item.get('started_at'),
                'finished_at': item.get('finished_at'),
                'statuses': set(),
                'earliest_started': item.get('started_at', ''),
                'latest_finished': item.get('finished_at', '')
            }
        
        entry = exec_map[pe]
        status = item.get('status', 'waiting')
        entry['statuses'].add(status)
        
        # Track earliest started_at and latest finished_at
        started = item.get('started_at', '')
        if started and (not entry['earliest_started'] or started < entry['earliest_started']):
            entry['earliest_started'] = started
        finished = item.get('finished_at', '')
        if finished and (not entry['latest_finished'] or finished > entry['latest_finished']):
            entry['latest_finished'] = finished
    
    executions = []
    for pe, data in exec_map.items():
        # Canonical derivation (ADR #112) — shared with /runs and the sidebar.
        status = derive_execution_status(data['statuses'])
        
        # Calculate duration for completed executions
        duration_ms = None
        if data['earliest_started'] and data['latest_finished']:
            try:
                start_dt = datetime.fromisoformat(data['earliest_started'].replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(data['latest_finished'].replace('Z', '+00:00'))
                duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
            except (ValueError, TypeError) as e:
                log.warn("pipelines_list", "Bad timestamp on execution; duration_ms left None",
                         execution_id=pe,
                         earliest_started=data.get('earliest_started'),
                         latest_finished=data.get('latest_finished'),
                         error=str(e))
        
        executions.append({
            'execution_id': pe,
            'execution_short': data['execution_short'],
            'date': data['date'],
            'status': status,
            'started_at': data['earliest_started'] or data['started_at'],
            'finished_at': data['latest_finished'] or data['finished_at'],
            'duration_ms': duration_ms
        })
    
    # Sort by started_at descending (newest first)
    executions.sort(key=lambda x: x.get('started_at') or '', reverse=True)
    return executions


def _query_pipeline_by_date_range(pipeline_name: str, start: datetime, end: datetime):
    """Query DDB date-pipeline-index GSI for a date range. Returns raw items."""
    items = []
    current = start
    while current.date() <= end.date():
        date_str = current.strftime('%Y-%m-%d')
        try:
            day_items = executions_repo.query_runs_by_date(
                date_str,
                min_rows=Limits.MAX_STATS_ITEMS,
                key_condition=Key('date').eq(date_str) & Key('pipeline_name').eq(pipeline_name),
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at',
                expr_names={'#d': 'date', '#s': 'status'}
            )
            items.extend(day_items)
        except (ClientError, BotoCoreError) as e:
            log.error("_query_pipeline_by_date_range", "Error querying date", error=str(e), date_str=date_str)
        current += timedelta(days=1)
    return items


def _reconcile_running(executions, pipeline_name: str):
    """For running executions, verify with SFN they're still alive.
    
    This is the ONLY place where UI reads from SFN API — to confirm
    running executions haven't silently failed. See DESIGN_DECISIONS.md #22.
    """
    running_execs = [e for e in executions if e.get('status') == 'running']
    if not running_execs:
        return
    
    # v0.78.14 (ADR #71) — use canonical normalize_execution_status helper
    # instead of a local mapping. Removes duplication; centralizes the
    # SFN UPPERCASE → canonical lowercase contract.
    # Note: previous local map collapsed TIMED_OUT → 'failed'. The canonical
    # helper preserves 'timed_out' as a distinct value. The terminal check
    # below (`!= 'running'`) catches both cases, so behavior is unchanged
    # for the SFN-failed-but-tasks-resolved recovery path.
    
    # Get pipeline ARN once for all reconcile calls
    pipeline_arn = ''
    try:
        pipeline_item = pipelines_repo.get(pipeline_name)
        if pipeline_item:
            pipeline_arn = pipeline_item.get('sfn_arn', '')
    except (ClientError, BotoCoreError) as e:
        log.warning("_reconcile_running", "Cannot fetch pipeline ARN", error=str(e))
        return  # Can't reconcile without ARN
    
    if not pipeline_arn:
        return
    
    for ex in running_execs:
        new_status = reconcile_sfn_status(ex['execution_id'], pipeline_arn)
        if new_status is not None:
            ex['status'] = new_status


def reconcile_sfn_status(execution_name, sfn_arn):
    """Query SFN for one execution and return its canonical status (ADR #112).

    Applies the recovered rule (SFN failed/timed_out but every task resolved) via the
    canonical ``reconcile_execution_status``. Returns None when the status can't be
    determined, so callers keep the DynamoDB-derived value. Shared by the pipeline
    execution list and the ``/runs`` feed so both surfaces reconcile identically.
    """
    if not sfn_arn:
        return None
    try:
        exec_arn = sfn_arn.replace(':stateMachine:', ':execution:') + ':' + execution_name
        desc = sfn.describe_execution(executionArn=exec_arn)
        sfn_status = normalize_execution_status(
            desc.get('status', ''),
            log_warn=lambda m, **ctx: log.warn("reconcile_sfn_status", m, **ctx),
        )
        all_resolved = False
        if sfn_status in ('failed', 'timed_out'):
            try:
                task_items = executions_repo.query_by_pipeline_execution(
                    execution_name, projection='#s', expr_names={'#s': 'status'})
                all_resolved = bool(task_items) and all(
                    i.get('status') in TASK_SUCCESS_STATUSES for i in task_items)
            except (ClientError, BotoCoreError) as e:
                # Can't verify recovery — treat as not resolved (ADR #38: still visible)
                log.warn("reconcile_sfn_status", "Failed to check task resolution",
                         execution_name=execution_name, error=str(e))
        return reconcile_execution_status('running', sfn_status, all_resolved)
    except (ClientError, BotoCoreError) as e:
        log.warn("reconcile_sfn_status", "Failed to describe SFN execution",
                 execution_name=execution_name, sfn_arn=sfn_arn, error=str(e))
        return None


def get_pipeline_executions(pipeline_name: str, event: Dict) -> Dict:
    """Get list of pipeline executions from DynamoDB (system of record).
    
    DATA SOURCE: DynamoDB (see DESIGN_DECISIONS.md #22).
    Uses date-pipeline-index GSI for efficient queries by logical date.
    SFN API only used for reconciliation of running executions.
    """
    # Get date filter from query params
    params = event.get('queryStringParameters', {}) or {}
    date_filter = params.get('date', '')
    start_date_filter = params.get('start_date', '')
    end_date_filter = params.get('end_date', '')
    
    try:
        items = []
        next_cursor = None  # only the no-date path paginates
        
        if date_filter:
            # Single date: one GSI query
            items = executions_repo.query_runs_by_date(
                date_filter,
                min_rows=Limits.MAX_STATS_ITEMS,
                key_condition=Key('date').eq(date_filter) & Key('pipeline_name').eq(pipeline_name),
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at',
                expr_names={'#d': 'date', '#s': 'status'}
            )
        elif start_date_filter or end_date_filter:
            # Date range (Calendar month view): query each date via GSI
            try:
                start = datetime.strptime(start_date_filter, '%Y-%m-%d') if start_date_filter else datetime.now(timezone.utc) - timedelta(days=30)
                end = datetime.strptime(end_date_filter, '%Y-%m-%d') if end_date_filter else datetime.now(timezone.utc)
                items = _query_pipeline_by_date_range(pipeline_name, start, end)
            except ValueError as e:
                return cors_response(400, {'error': f'Invalid date format: {e}'})
        else:
            # No date filter: this pipeline's runs, newest first, via
            # pipeline-date-index. One indexed query instead of a day-by-day
            # loop, so there is no window constant — depth is bounded only by
            # the row TTL. `before` is an opaque cursor (a date, inclusive).
            items, next_cursor = executions_repo.query_runs_by_pipeline(
                pipeline_name,
                min_runs=safe_param_int(params, 'page_size', Limits.RUNS_PAGE_SIZE, 100),
                before_date=params.get('before', '') or None,
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at',
                expr_names={'#d': 'date', '#s': 'status'}
            )
        
        executions = _aggregate_executions(items)
        
        # Reconcile running executions with SFN (verify still alive)
        _reconcile_running(executions, pipeline_name)
        
        # Get pipeline ARN for response (informational)
        pipeline_arn = ''
        try:
            pipeline_item = pipelines_repo.get(pipeline_name)
            if pipeline_item:
                pipeline_arn = pipeline_item.get('sfn_arn', '')
        except (ClientError, BotoCoreError):
            pass  # ARN is informational — response works without it
        
        return cors_response(200, {
            'pipeline': pipeline_name,
            'pipeline_arn': pipeline_arn,
            'executions': executions,
            # Opaque cursor for the next (older) page; None when nothing older
            # exists. Only the no-date path paginates — an explicit date or range
            # is already bounded by the caller.
            'next': next_cursor,
        })
    
    except Exception as e:
        log.error("get_pipeline_executions", "Error", error=str(e))
        return cors_response(500, {'error': f'Failed to list executions: {str(e)}'})


def register(router) -> None:
    """Register pipeline list/status routes. See ADR #97."""
    router.add('GET', '/api/pipelines', list_pipelines)
    router.add('GET', '/api/pipeline-status', get_pipeline_status, 'name')
    router.add('GET', '/api/pipeline-executions', get_pipeline_executions, 'name')
