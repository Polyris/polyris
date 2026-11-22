"""Pipeline listing and status routes.

Handles:
- list_pipelines: List all pipelines with their latest run, recent runs and SLA
- get_pipeline_status: One run's tasks: the run asked for, or the latest run of a date
- get_pipeline_executions: A pipeline's runs: its history, one date's, or a range of dates

Every run list reads run rows (ADR #137 §5); no route scans the tasks table or asks
Step Functions about a run.
"""
import json
from datetime import datetime, timezone, timedelta
from typing import Dict

from botocore.exceptions import ClientError, BotoCoreError
from polyris.constants import ACCESS_DENIED_CODES
from polyris.coordination import attention, task_row, task_spec, trigger_rules

from config import sfn
from dal import executions_repo, pipelines_repo
from dal.specs_repo import needs_redeploy
from constants import Limits, TaskStatus
from feed import (feed_dates, latest_run_of_date, page_by_started_at, run_counts, run_entry, run_rows_before,
                  runs_of_date)
from response import cors_response, index_building_response
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

    # An empty registry (pipelines that ran but never registered): discover them from one page of the tokens
    if not pipelines:
        try:
            for name in executions_repo.discover_pipeline_names():
                if name not in pipelines:
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


def _readiness(item: Dict) -> Dict:
    """A 1.3 row's readiness fields (dependencies, trigger rule, wait_before, wait_for), or {} for a 1.2 row."""
    text = item.get(task_row.READINESS)
    if not text:
        return {}
    try:
        return task_spec.readiness_from_text(str(text))
    except (ValueError, TypeError) as e:
        log.warn("pipeline_status", "A task row's readiness cannot be read", execution_name=item.get('execution_name'),
                 error=str(e))
        return {}


def _why_it_waits(item: Dict, readiness: Dict, rows: Dict[str, Dict]) -> Dict:
    """Why a task waits (ADR #137 §1): the reasons the dispatcher recorded and since when, the end of its
    ``wait_before`` countdown or the deadline of its wait or decision, and what its trigger rule makes of its upstreams
    as the page shows them (``trigger_rules.evaluate``). No route of its own: the page already reads every row of the
    run."""
    status = item.get('status')
    if status not in ('waiting', 'waiting_paused', 'waiting_decision'):
        return {}
    due = item.get(attention.DUE)
    # A ready task that counts down its wait_before falls due when the countdown ends, when it starts: the console's
    # countdown, no deadline (review of TR-6d). A deadline a pause holds falls due far in the future: the page shows
    # none (ADR #137 §5).
    countdown_end = item.get(task_row.COUNTDOWN_END) if status == 'waiting' else None
    detail: Dict = {
        'reasons': [str(r) for r in item.get(task_row.REASONS) or []],
        'reasons_at': item.get(task_row.REASONS_AT),
        'countdown_end': countdown_end,
        'deadline': due if due and not countdown_end and not str(due).startswith('9999') else None,
    }
    dependencies = readiness.get('dependencies') or []
    if status != 'waiting_decision' and dependencies and all(d in rows for d in dependencies):
        upstreams = [rows[d] for d in dependencies]
        verdict = trigger_rules.evaluate(readiness.get('trigger_rule') or 'all_success',
                                         [str(u.get('status', '')) for u in upstreams],
                                         [u.get(task_row.SKIP_ORIGIN) for u in upstreams])
        detail['dependency_verdict'] = {
            'verdict': verdict.verdict, 'reason': verdict.reason,
            'upstreams': [{'name': d, 'status': rows[d].get('status'),
                           'counts_as': trigger_rules.counts_as(str(rows[d].get('status', '')),
                                                                rows[d].get(task_row.SKIP_ORIGIN))}
                          for d in dependencies]}
    return detail


def get_pipeline_status(pipeline_name: str, event: Dict) -> Dict:
    """One run's tasks for the pipeline page: the run asked for, or the latest run of the date.

    The latest run of a date is found through the pipeline's task rows of that date, a
    query of pipeline-date-index; then its rows are read in full by the run. No scan of
    the table and no call to Step Functions (ADR #137 §5).
    """
    params = event.get('queryStringParameters') or {}
    date = params.get('date') or datetime.now(timezone.utc).strftime('%Y-%m-%d')
    selected_execution = params.get('pipeline_execution', '') or latest_run_of_date(pipeline_name, date)
    items = executions_repo.query_by_pipeline_execution(selected_execution) if selected_execution else []

    # One row per task of the run.
    task_map = {}
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
        task_map[task_name] = item
    if not task_map:
        selected_execution = None

    # Now build the final task list
    tasks = []
    for task_name, item in task_map.items():
        readiness = _readiness(item)
        # Parse dependencies: a 1.3 row holds them in its readiness (ADR #137 §8), a 1.2 row as JSON text
        deps_str = item.get('dependencies', '[]')
        try:
            deps = readiness.get('dependencies') if readiness else (
                json.loads(deps_str) if isinstance(deps_str, str) else deps_str)
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
            'dependencies': deps or [],
            'wait_for': json.dumps(readiness['wait_for']) if readiness.get('wait_for') else item.get('wait_for', '[]'),
            'started_at': item.get('started_at', ''),
            'running_at': item.get('running_at'),
            'finished_at': item.get('finished_at'),
            'error': item.get('error'),
            'result': result,
            'wait_before': parse_wait_before(readiness.get('wait_before', item.get('wait_before'))),
            'wait_delay_started_ms': safe_int(item.get('wait_delay_started_ms')),
            'wait_delay_until_ms': safe_int(item.get('wait_delay_until_ms')),
            'trigger_rule': readiness.get('trigger_rule') or item.get('trigger_rule', 'all_success'),
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
            **_why_it_waits(item, readiness, task_map),
        })
    
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

def get_pipeline_executions(pipeline_name: str, event: Dict) -> Dict:
    """A pipeline's runs for its page (ADR #137 §5, D100): its history, one date's runs, or a range of dates.

    Without a date it reads the pipeline's run rows from run-start-index, the start days
    of the last ``Limits.RUNS_WINDOW_DAYS`` days newest first, a page at a time
    (``before``/``next``, opaque). A range (``start_date``/``end_date``) walks every start
    day of that window with a filter on the run's logical date too: a run can start
    before its date, or long after it. One ``date`` reads that date's task rows and the
    run rows of its runs, runs from before 1.3 included (``feed.runs_of_date``). Each
    run's status comes from its run row's summary; only an opened run reads its task rows.
    """
    params = event.get('queryStringParameters', {}) or {}
    date_filter = params.get('date', '')
    start_date_filter = params.get('start_date', '')
    end_date_filter = params.get('end_date', '')
    before = params.get('before', '')

    try:
        next_cursor = None  # only the history pages
        if date_filter:
            runs = runs_of_date(date_filter, pipeline_name)
        elif start_date_filter or end_date_filter:
            try:
                today = datetime.now(timezone.utc)
                first = (datetime.strptime(start_date_filter, '%Y-%m-%d') if start_date_filter
                         else today - timedelta(days=30)).strftime('%Y-%m-%d')
                last = (datetime.strptime(end_date_filter, '%Y-%m-%d') if end_date_filter
                        else today).strftime('%Y-%m-%d')
            except ValueError as e:
                return cors_response(400, {'error': f'Invalid date format: {e}'})
            runs = [run_entry(item) for item in run_rows_before(
                '', None, days=Limits.RUNS_WINDOW_DAYS, pipeline=pipeline_name, run_dates=(first, last))[0]]
        else:
            page_size = safe_param_int(params, 'page_size', Limits.RUNS_PAGE_SIZE, 100)
            rows, more = run_rows_before(before, page_size + 1, days=Limits.RUNS_WINDOW_DAYS, pipeline=pipeline_name)
            runs, next_cursor = page_by_started_at([run_entry(item) for item in rows], before, page_size, more=more)
        runs.sort(key=lambda r: r.get('started_at') or '', reverse=True)

        # Get pipeline ARN for response (informational)
        pipeline_arn = ''
        try:
            pipeline_item = pipelines_repo.get(pipeline_name)
            if pipeline_item:
                pipeline_arn = pipeline_item.get('sfn_arn', '')
        except (ClientError, BotoCoreError) as e:
            # The ARN is informational: the runs answer without it.
            log.warn("get_pipeline_executions", "Could not read the pipeline's ARN", error=str(e))

        return cors_response(200, {
            'pipeline': pipeline_name,
            'pipeline_arn': pipeline_arn,
            'executions': [_execution(run) for run in runs],
            # Opaque cursor for the next (older) page; None when nothing older exists.
            # Only the history pages: a date or a range is bounded by the caller.
            'next': next_cursor,
        })
    except executions_repo.IndexBuilding:
        return index_building_response()
    except Exception as e:
        log.error("get_pipeline_executions", "Error", error=str(e))
        return cors_response(500, {'error': f'Failed to list executions: {str(e)}'})


def _execution(run: Dict) -> Dict:
    """A run of the run lists (``feed.run_entry``) as the pipeline page names its fields."""
    return {
        'execution_id': run['pipeline_execution'],
        'execution_short': run['pipeline_execution_short'],
        'date': run['date'],
        'status': run['status'],
        'failed_tasks': run['failed_tasks'],
        'failed_step': run.get('failed_step'),
        'started_at': run['started_at'],
        'finished_at': run['finished_at'],
        'duration_ms': run['duration_ms'],
        'triggered_by': run['triggered_by'],
    }


def register(router) -> None:
    """Register pipeline list/status routes. See ADR #97."""
    router.add('GET', '/api/pipelines', list_pipelines)
    router.add('GET', '/api/pipeline-status', get_pipeline_status, 'name')
    router.add('GET', '/api/pipeline-executions', get_pipeline_executions, 'name')
