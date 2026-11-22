"""Pipeline listing and status routes.

Handles:
- list_pipelines: List all pipelines with their latest run, recent runs and SLA
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
from polyris.constants import ACCESS_DENIED_CODES

from config import sfn
from dal import executions_repo, pipelines_repo
from dal.specs_repo import needs_redeploy
from constants import Limits, normalize_execution_status, derive_execution_status, reconcile_execution_status, TASK_SETTLED_STATUSES, TASK_SUCCESS_STATUSES, TaskStatus
from feed import feed_dates, fill_triggered_by, run_counts, run_entry
from response import cors_response
from logger import log
from utils import safe_int, safe_param_int, retrieve_result, parse_wait_before, should_skip_token_row, resolve_max_retries

def list_pipelines(event: Dict) -> Dict:
    """List the registered pipelines; with ``?stats=true`` each one's latest run, recent runs and SLA.

    The registry is the source of the list (a small table). The stats read the run
    rows of the last ``Limits.SLA_DAYS`` start days from run-start-index (ADR #137 §5,
    D94), each run's status from its run row's summary; the console asks Step
    Functions nothing about a run. ``stats_incomplete`` says a start day could not be
    read, and ``index_building`` that DynamoDB is still building the index.
    """
    params = event.get('queryStringParameters', {}) or {}
    include_stats = params.get('stats', 'false').lower() == 'true'

    pipelines = {}

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
                    'registered_at': item.get('registered_at', ''),
                    # Deployed with an older format than this console reads, or before 1.3
                    # (ADR #137, *Migration*): its runs fail until polyris-deploy runs again.
                    'needs_redeploy': needs_redeploy(item),
                    'status': 'idle',
                    'paused': False,
                    'sla': None,
                    'recent_runs': None,
                    # The latest run waits for an operator (ADR #112 §7 follow-up): a
                    # TaskStatus, waiting_decision or waiting_paused; None otherwise.
                    'waiting_on': None,
                    # How many tasks of the latest run failed (ADR #137 §4); None without a run.
                    'failed_tasks': None,
                }
    except (ClientError, BotoCoreError) as e:
        # A list without the registry has no groups or schedules and misses every pipeline
        # without a recent run: an answer that looks whole and is not (ADR #136). Fail instead.
        log.error("list_pipelines", "Error scanning registry", error=str(e))
        raise

    # An empty registry (pipelines that ran but never registered): discover them from the tokens
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
                        'needs_redeploy': True,  # it has no registry entry, so no format either
                        'status': 'idle',
                        'paused': False,
                        'sla': None,
                        'waiting_on': None,
                        'failed_tasks': None,
                    }
        except (ClientError, BotoCoreError) as e:
            # Else an unreadable table reads "No pipelines registered yet"
            log.error("list_pipelines", "Error scanning tokens for discovery", error=str(e))
            raise

    # 2. The stats: the run rows of the last SLA_DAYS start days, read one day at a time so a day that cannot
    #    be read leaves the others (ADR #136 §4, ADR #137 §5).
    stats_incomplete = False
    index_building = False
    run_lists: Dict[str, list] = {}  # {pipeline_name: [run as run_entry shows it]}, newest first
    if include_stats and pipelines:
        oldest_date = feed_dates('')[-1]
        for start_day in feed_dates(''):
            try:
                items = executions_repo.runs_started(start_day)
            except executions_repo.IndexBuilding:
                index_building = stats_incomplete = True
                break
            except (ClientError, BotoCoreError) as e:
                log.error("list_pipelines", "Error reading the runs of a start day", error=str(e),
                          start_day=start_day)
                if _access_denied(e):
                    raise
                stats_incomplete = True
                continue
            for item in items:
                run = run_entry(item)
                # recent_runs keeps its meaning: runs whose logical date is within the window (ADR #136).
                if run['pipeline_name'] in pipelines and (run['date'] or '') >= oldest_date:
                    run_lists.setdefault(run['pipeline_name'], []).append((run, run_counts(item)))
        for name, runs in run_lists.items():
            # Newest first by date, then by start: the date alone left runs of one date in index order.
            runs.sort(key=lambda r: (r[0]['date'] or '', r[0]['started_at'] or ''), reverse=True)
            _finish_runs(pipelines[name], runs)

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
        'pipelines': list(pipelines.values()),
        'stats_incomplete': stats_incomplete,
        'index_building': index_building,
    })


def _access_denied(e: Exception) -> bool:
    """A missing permission, raised rather than folded into "could not read" (CLAUDE.md, Error Visibility)."""
    return isinstance(e, ClientError) and e.response.get('Error', {}).get('Code') in ACCESS_DENIED_CODES


#: A run still running whose rows include one of these waits for an operator (ADR #136).
_WAITING_ON_OPERATOR = (TaskStatus.WAITING_DECISION, TaskStatus.WAITING_PAUSED)


def _finish_runs(pipeline: Dict, runs: list) -> None:
    """Set a pipeline's status, sparkline, SLA and ``waiting_on`` from its runs and their summaries, newest first.

    The card's status is the latest run's (ADR #112), and so is ``waiting_on`` while it runs; a run that is still
    running says how many of its tasks failed (ADR #137 §4).
    """
    latest, counts = runs[0]
    if latest['status'] == 'running':
        pipeline['waiting_on'] = next((s for s in _WAITING_ON_OPERATOR if (counts or {}).get(s)), None)
    pipeline['failed_tasks'] = latest['failed_tasks']

    # SLA: % of completed runs that succeeded. Running runs are excluded; aborted (user-stopped) runs are
    # excluded too: a stop is not a system failure (ADR #112).
    completed = [run['status'] for run, _ in runs if run['status'] not in ('running', 'aborted')]
    if completed:
        pipeline['sla'] = round(sum(1 for status in completed if status == 'success') / len(completed) * 100)

    pipeline['recent_runs'] = [{
        'date': run['date'], 'started_at': run['started_at'], 'exec': run['pipeline_execution'][-8:],
        'status': run['status'], 'failed_tasks': run['failed_tasks'],
    } for run, _ in runs[:10]]
    pipeline['status'] = latest['status']


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
            'attempt': safe_int(item.get('attempt')) if item.get('attempt') is not None else None,
            # retry_attempt (ADR #125) — in-SFN auto-retry index for the current wrapper.
            # Distinct from `attempt` which bumps only on manual Restart.
            'retry_attempt': safe_int(item.get('retry_attempt'), 0),
            'max_retries': resolve_max_retries(item),
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
                'latest_finished': item.get('finished_at', ''),
                'triggered_by': item.get('triggered_by', ''),
                # execution_name (base table PK) is always returned by GSI queries;
                # used by _fill_triggered_by to GetItem triggered_by from base table
                # when the GSI INCLUDE projection doesn't carry it.
                '_sample_exec_name': item.get('execution_name', ''),
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
            'duration_ms': duration_ms,
            'triggered_by': data.get('triggered_by') or None,
            '_sample_exec_name': data.get('_sample_exec_name', ''),
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
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at, execution_name',
                expr_names={'#d': 'date', '#s': 'status'}
            )
            items.extend(day_items)
        except (ClientError, BotoCoreError) as e:
            log.error("_query_pipeline_by_date_range", "Error querying date", error=str(e), date_str=date_str)
        current += timedelta(days=1)
    return items


def _reconcile_running(executions, pipeline_name: str):
    """For running executions, verify with SFN they're still alive.
    
    The console reads Step Functions only to confirm that a run its tasks call running is
    still alive (ADR #22, #112 §7): here, in list_pipelines for each pipeline's latest run,
    and in the /runs feed.
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


def check_with_sfn(execution_name, sfn_arn):
    """The canonical status of a run whose tasks say running, from Step Functions (ADR #112).

    Applies the recovered rule (SFN failed/timed_out but every task resolved) via the
    canonical ``reconcile_execution_status``. Raises ClientError or BotoCoreError when Step
    Functions cannot answer; the caller decides what that means.
    """
    exec_arn = sfn_arn.replace(':stateMachine:', ':execution:') + ':' + execution_name
    desc = sfn.describe_execution(executionArn=exec_arn)
    sfn_status = normalize_execution_status(
        desc.get('status', ''),
        log_warn=lambda m, **ctx: log.warn("reconcile_sfn_status", m, **ctx),
    )
    all_resolved = False
    if sfn_status in ('failed', 'timed_out'):
        try:
            # execution_name lets should_skip_token_row drop the internal rows that share the run:
            # warnings (status failed), the pause record and the DAG snapshot (no status).
            items = executions_repo.query_by_pipeline_execution(
                execution_name, projection='execution_name, #s', expr_names={'#s': 'status'})
            task_items = [i for i in items if not should_skip_token_row(i)]
            all_resolved = bool(task_items) and all(
                i.get('status') in TASK_SUCCESS_STATUSES for i in task_items)
        except (ClientError, BotoCoreError) as e:
            if _access_denied(e):
                raise
            # Can't verify recovery — treat as not resolved (ADR #38: still visible)
            log.warn("reconcile_sfn_status", "Failed to check task resolution",
                     execution_name=execution_name, error=str(e))
    return reconcile_execution_status('running', sfn_status, all_resolved)


def reconcile_sfn_status(execution_name, sfn_arn):
    """check_with_sfn, or None when the status can't be determined (ADR #112).

    Callers keep the DynamoDB-derived value on None. Shared by the pipeline execution list
    and the ``/runs`` feed so both surfaces reconcile identically. A denied call is raised.
    """
    if not sfn_arn:
        return None
    try:
        return check_with_sfn(execution_name, sfn_arn)
    except (ClientError, BotoCoreError) as e:
        if _access_denied(e):
            log.error("reconcile_sfn_status", "Step Functions check denied",
                      execution_name=execution_name, sfn_arn=sfn_arn, error=str(e))
            raise
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
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at, execution_name',
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
                projection='pipeline_execution, pipeline_execution_short, #d, #s, started_at, finished_at, execution_name',
                expr_names={'#d': 'date', '#s': 'status'}
            )

        executions = _aggregate_executions(items)

        # Fetch triggered_by from base table (GSI INCLUDE doesn't project it).
        # Pops _sample_exec_name from each execution as a side effect.
        fill_triggered_by(executions)

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
