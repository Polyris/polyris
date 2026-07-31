"""Execution routes for Console API.

Handles pipeline execution control operations:
- get_all_runs: List all pipeline executions
- stop_execution: Stop a running execution
- pause_execution: Pause execution (running tasks complete, new tasks wait)
- resume_execution: Resume paused execution
- extend_pause: Extend pause timeout by 12 hours
- get_execution_pause_status: Check if execution is paused
- get_execution_children: Get child tasks of an execution
- get_execution_parent: Get parent execution info
"""
import json
from datetime import datetime, timezone
from typing import Dict, List

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError, BotoCoreError

from config import sfn
from dal import executions_repo, pipelines_repo, backfills_repo
from .pipelines_list import reconcile_sfn_status
from dal.subscriptions_repo import dep_subscriptions_repo
from constants import Limits, TASK_SETTLED_STATUSES, derive_execution_status
from feed import feed_dates, is_older, page_by_started_at, pipeline_rows_before
from response import cors_response
from logger import log
from utils import safe_param_int, should_skip_token_row


def _backfill_matches(bf: Dict, status_filter: str, pipeline_filter: str,
                      date_filter: str) -> bool:
    """The feed's filter semantics for one Backfill record (ADR #95 decisions).

      * ``status_filter`` — literal match against the Backfill status.
      * ``pipeline_filter`` — match on ``target_pipeline`` (cross-pipeline
        backfills surface under their target only).
      * ``date_filter`` — include when the date is within the backfill's
        partition range (string range over ``partition_keys``; daily-oriented).
    """
    if status_filter and (bf.get('status', 'pending') != status_filter):
        return False

    if pipeline_filter and (bf.get('target_pipeline', '') or '') != pipeline_filter:
        return False

    if date_filter:
        try:
            keys = json.loads(bf.get('partition_keys') or '[]')
        except (json.JSONDecodeError, TypeError):
            keys = []
        if not keys or not (min(keys) <= date_filter <= max(keys)):
            return False

    return True


def _build_backfill_run_rows(
    limit: int,
    status_filter: str,
    pipeline_filter: str,
    date_filter: str,
    before: str = '',
) -> List[Dict]:
    """Build unified-feed rows for Backfills (``kind='backfill'``) — ADR #95.

    Backfills live in pipeline-tokens under a sentinel ``pipeline_name`` and are
    excluded from the execution rows in ``get_all_runs`` by
    ``should_skip_token_row``; they are merged into ``/api/runs`` here as
    first-class rows. Status stays in the 6-state Backfill vocabulary (ADR #56)
    — no normalization to execution statuses; the ``kind`` field lets the UI
    interpret it. Children are not embedded: a backfill row expands on demand
    via ``GET /api/backfills/by-id``.

    ``before`` is the feed's paging cursor (see ``feed.py``) and is handed to the repo
    rather than applied here: ``list_recent`` sorts newest-first, so a cursor applied
    after a ``limit`` slice would return nothing on every page but the first, and
    Backfills would quietly vanish from the feed as soon as you paged.

    **Filters run before the page is cut, and the repo is asked for no limit at all.**
    ``list_recent`` scans the sentinel unconditionally — its ``limit`` buys no capacity,
    it only truncates an already-materialised list — so slicing first just spends the
    page on records the filters are about to drop. ``?status=completed`` with a run of
    recent ``partial`` backfills would then answer "no runs found" while completed ones
    sat right behind them, and an empty page has no last row, so no cursor, so the feed
    would stop there for good. ``limit + 1`` for the same reason
    ``feed.pipeline_rows_before`` overshoots: the page learns it is not the last one by
    having more rows than it can fit.

    Degrades gracefully: on a DDB error the executions are still returned.
    """
    rows: List[Dict] = []
    try:
        backfills = backfills_repo.list_recent(limit=None, before=before or None)
    except (ClientError, BotoCoreError) as e:
        log.error("get_all_runs", "Failed to list backfills for unified feed", error=str(e))
        return rows

    matching = [bf for bf in backfills
                if _backfill_matches(bf, status_filter, pipeline_filter, date_filter)]

    for bf in matching[:limit + 1]:
        started_at = bf.get('started_at', '') or ''
        finished_at = bf.get('finished_at', '') or ''
        duration_ms = None
        if started_at and finished_at:
            try:
                start_dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                end_dt = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
                duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
            except (ValueError, TypeError) as e:
                log.warn("get_all_runs", "Bad timestamp on backfill; duration_ms left None",
                         backfill_id=bf.get('backfill_id'), error=str(e))

        rows.append({
            'kind': 'backfill',
            'id': bf.get('backfill_id'),
            'backfill_id': bf.get('backfill_id'),
            'pipeline_name': bf.get('target_pipeline', '') or '',
            'status': bf.get('status', 'pending'),
            'started_at': started_at or None,
            'finished_at': finished_at or None,
            'started_by': bf.get('started_by'),
            'total_partitions': int(bf.get('total_partitions', 0) or 0),
            'completed_partitions': int(bf.get('completed_partitions', 0) or 0),
            'failed_partitions': int(bf.get('failed_partitions', 0) or 0),
            'skipped_partitions': int(bf.get('skipped_partitions', 0) or 0),
            'downstream': bf.get('cascade'),
            'granularity': bf.get('granularity'),
            'date': None,
            'duration_ms': duration_ms,
        })
    return rows


_RUNS_PROJECTION = ('pipeline_execution, pipeline_execution_short, pipeline_name, '
                    '#d, #s, started_at, finished_at')
_RUNS_EXPR_NAMES = {'#d': 'date', '#s': 'status'}


def get_all_runs(event: Dict) -> Dict:
    """Get all pipeline runs across pipelines from DynamoDB.
    
    DATA SOURCE: DynamoDB (see DESIGN_DECISIONS.md #22).
    Uses date-pipeline-index GSI for efficient queries by logical date.

    Pages with an opaque ``before`` cursor and answers with ``next`` (see ``feed.py``),
    so the row count is what the feed actually holds rather than whatever survived a
    silent slice. Backfills page through the same cursor — they carry ``started_at``
    too, which is what makes one cursor enough for the merged feed (ADR #95).
    """
    params = event.get('queryStringParameters', {}) or {}
    limit = safe_param_int(params, 'limit', Limits.RUNS_FEED_LIMIT, 200)
    before = params.get('before', '')
    date_filter = params.get('date', '')
    pipeline_filter = params.get('pipeline', '')
    status_filter = params.get('status', '')
    
    try:
        all_items = []
        
        if date_filter:
            # Single date: one GSI query. Already bounded, so the cursor only pages
            # what came back — no narrowing to do on the read itself.
            key_cond = Key('date').eq(date_filter)
            if pipeline_filter:
                key_cond = key_cond & Key('pipeline_name').eq(pipeline_filter)
            all_items = executions_repo.query_runs_by_date(
                date_filter,
                min_rows=Limits.RUNS_MIN_ROWS_PER_DATE,
                key_condition=key_cond,
                projection=_RUNS_PROJECTION,
                expr_names=_RUNS_EXPR_NAMES,
            )
        elif pipeline_filter:
            # One pipeline, no date: ask pipeline-date-index directly (ADR #108).
            # No day-loop and no window — how far back you can see is bounded by
            # the row TTL, not by SLA_DAYS. The cross-pipeline branch below cannot
            # do this: with no pipeline to hash on, `date` is the only shard key.
            all_items = pipeline_rows_before(
                pipeline_filter, before, limit,
                # A run is older than the cursor iff any of its rows is: the run's
                # started_at is the earliest of them.
                count_fn=lambda rows: len({
                    r.get('pipeline_execution') for r in rows
                    if r.get('pipeline_execution')
                    and not should_skip_token_row(r)
                    and is_older(r, before)
                }),
                projection=_RUNS_PROJECTION,
                expr_names=_RUNS_EXPR_NAMES,
            )
        else:
            # Default: the SLA window, walked back from the cursor's date. This feed
            # is a cross-pipeline time series, so `date` is the natural shard key and
            # a per-date fan-out is the correct access pattern here (unlike the
            # per-pipeline path, which uses pipeline-date-index and needs no window
            # at all).
            for date_str in feed_dates(before):
                try:
                    day_items = executions_repo.query_runs_by_date(
                        date_str,
                        min_rows=Limits.FEED_MIN_ROWS_PER_DAY,
                        projection=_RUNS_PROJECTION,
                        expr_names=_RUNS_EXPR_NAMES,
                    )
                    all_items.extend(day_items)
                except Exception as e:
                    log.error("get_all_runs", "Error querying date", error=str(e), date_str=date_str)
        
        # Group by pipeline_execution and aggregate task statuses.
        exec_map = {}
        for item in all_items:
            # Skip internal/special records (_pause_, _notify_warn_, etc.)
            if should_skip_token_row(item):
                continue
            pe = item.get('pipeline_execution')
            p_name = item.get('pipeline_name', '')
            if not pe or pe == 'unknown' or not p_name or p_name == 'unknown':
                continue
            
            if pe not in exec_map:
                exec_map[pe] = {
                    'pipeline_name': p_name,
                    'pipeline_execution': pe,
                    'pipeline_execution_short': item.get('pipeline_execution_short', pe[:20] if pe else ''),
                    'date': item.get('date'),
                    'started_at': item.get('started_at', ''),
                    'finished_at': item.get('finished_at', ''),
                    'statuses': set()
                }
            
            entry = exec_map[pe]
            entry['statuses'].add(item.get('status', 'waiting'))
            
            started = item.get('started_at', '')
            if started and (not entry['started_at'] or started < entry['started_at']):
                entry['started_at'] = started
            finished = item.get('finished_at', '')
            if finished and (not entry['finished_at'] or finished > entry['finished_at']):
                entry['finished_at'] = finished
        
        all_runs = []
        for pe, data in exec_map.items():
            # Canonical derivation (ADR #112) — one source shared with all surfaces.
            status = derive_execution_status(data['statuses'])

            # Calculate duration
            duration_ms = None
            if data['started_at'] and data['finished_at']:
                try:
                    start_dt = datetime.fromisoformat(data['started_at'].replace('Z', '+00:00'))
                    end_dt = datetime.fromisoformat(data['finished_at'].replace('Z', '+00:00'))
                    duration_ms = int((end_dt - start_dt).total_seconds() * 1000)
                except (ValueError, TypeError) as e:
                    log.warn("get_all_runs", "Bad timestamp on run; duration_ms left None",
                             pipeline_execution=pe,
                             started_at=data.get('started_at'),
                             finished_at=data.get('finished_at'),
                             error=str(e))

            all_runs.append({
                'kind': 'execution',
                'pipeline_name': data['pipeline_name'],
                'pipeline_execution': pe,
                'pipeline_execution_short': data['pipeline_execution_short'],
                'status': status,
                'started_at': data['started_at'] or None,
                'finished_at': data['finished_at'] or None,
                'date': data['date'],
                'duration_ms': duration_ms
            })

        # Drop what earlier pages already served before reconciling: a run newer than
        # the cursor cannot appear on this page, and reconciling it would spend an SFN
        # describe on a row nobody will see. The paging contract is still enforced
        # below, in page_by_started_at — this is only about not paying for it twice.
        all_runs = [r for r in all_runs if is_older(r, before)]

        # Reconcile 'running' executions against SFN (ADR #112, decision b) so /runs and
        # the execution dropdown agree on stuck-running / timed_out / recovered. ARNs are
        # resolved once per pipeline; only running rows incur an SFN call. Runs BEFORE the
        # status filter so a reconciled status filters correctly.
        arn_cache = {}
        for r in all_runs:
            if r['status'] != 'running':
                continue
            pname = r['pipeline_name']
            if pname not in arn_cache:
                try:
                    item = pipelines_repo.get(pname)
                    arn_cache[pname] = item.get('sfn_arn', '') if item else ''
                except (ClientError, BotoCoreError):
                    arn_cache[pname] = ''
            new_status = reconcile_sfn_status(r['pipeline_execution'], arn_cache[pname])
            if new_status is not None:
                r['status'] = new_status

        if status_filter:
            all_runs = [r for r in all_runs if r['status'] == status_filter]

        
        # Merge Backfills as first-class 'backfill' rows (ADR #95). Backfills
        # are excluded from the execution rows above by should_skip_token_row,
        # so there is no double-count — they enter only here.
        all_runs.extend(
            _build_backfill_run_rows(limit, status_filter, pipeline_filter, date_filter, before)
        )

        # One page, newest first, plus the cursor for the older one. Both kinds sort
        # and page on the same started_at, so the merge needs no second cursor.
        page, next_cursor = page_by_started_at(all_runs, before, limit)

        return cors_response(200, {
            'runs': page,
            'count': len(page),
            # Opaque cursor for the next (older) page; None when nothing older exists.
            'next': next_cursor,
            'filters': {
                'pipeline': pipeline_filter,
                'status': status_filter,
                'date': date_filter
            }
        })
    except Exception as e:
        log.error("get_all_runs", "Error", error=str(e))
        return cors_response(500, {'error': f'Failed to get runs: {str(e)}'})


def get_execution_children(execution_name: str, event: Dict) -> Dict:
    """
    Get child executions of a parent execution.
    Uses GSI pipeline-execution-index on pipeline_tokens table.
    
    Includes pagination to handle >1MB of results.
    """
    try:
        # Query GSI for children (repo handles pagination)
        items = executions_repo.query_by_pipeline_execution(execution_name)
        
        children = []
        for item in items:
            if should_skip_token_row(item):
                continue
            children.append({
                'execution_name': item.get('execution_name'),
                'task_name': item.get('task_name', ''),
                'status': item.get('status'),
                'date': item.get('date'),
                'started_at': item.get('started_at'),
                'finished_at': item.get('finished_at')
            })
        
        # Sort by started_at
        children.sort(key=lambda x: x.get('started_at', ''))
        
        return cors_response(200, {
            'pipeline_execution': execution_name,
            'children': children,
            'count': len(children)
        })
    
    except Exception as e:
        log.error("get_execution_children", "Error getting children", error=str(e), execution_name=execution_name)
        return cors_response(200, {
            'pipeline_execution': execution_name,
            'children': [],
            'count': 0
        })


def get_execution_parent(execution_name: str, event: Dict) -> Dict:
    """
    Get parent execution info for a given execution.
    """
    try:
        # Get the execution record
        item = executions_repo.get(execution_name)
        
        if not item:
            return cors_response(404, {'error': 'Execution not found'})
        
        pipeline_execution = item.get('pipeline_execution', 'none')
        pipeline_name = item.get('pipeline_name', 'none')
        parent_task = item.get('task_name', 'none')
        
        # If no parent, return empty
        if pipeline_execution == 'none' or not pipeline_execution:
            return cors_response(200, {
                'execution_name': execution_name,
                'has_parent': False,
                'parent': None
            })
        
        # Get parent execution details
        parent_item = executions_repo.get(pipeline_execution)
        
        parent_info = None
        if parent_item:
            parent_info = {
                'execution_name': pipeline_execution,
                'pipeline': pipeline_name,
                'task': parent_task,
                'status': parent_item.get('status'),
                'date': parent_item.get('date')
            }
        else:
            # Parent record might be expired/deleted, just return what we know
            parent_info = {
                'execution_name': pipeline_execution,
                'pipeline': pipeline_name,
                'task': parent_task,
                'status': 'unknown',
                'date': ''
            }
        
        return cors_response(200, {
            'execution_name': execution_name,
            'has_parent': True,
            'parent': parent_info
        })
    
    except Exception as e:
        log.error("executions", "Error getting parent", error=str(e), execution_name=execution_name)
        return cors_response(500, {'error': str(e)})



def _cleanup_dependency_subscriptions(subscriber_name: str, dependency_keys: List[str]) -> None:
    """
    Clean up dependency subscriptions after resume.

    When a task was waiting for deps and got paused (with wait_token),
    we stored pending_dependency_keys. After resume, we should clean
    these subscriptions to prevent stale data.
    """
    if not dependency_keys:
        return

    for dep_key in dependency_keys:
        try:
            dep_subscriptions_repo.delete(dep_key, subscriber_name)
        except Exception as e:
            # Non-critical - log and continue
            log.error(
                "_cleanup_dependency_subscriptions",
                "Failed to delete subscription",
                error=str(e),
                dep_key=dep_key,
                subscriber_name=subscriber_name,
            )


def _sfn_stop(arn: str, context: str = '') -> None:
    """Stop a single SFN execution, ignoring not-found / already-stopped errors."""
    if not arn or not arn.startswith('arn:aws:states:'):
        return
    try:
        sfn.stop_execution(executionArn=arn, error='StoppedViaUI', cause='Pipeline stopped via UI')
    except Exception as e:
        log.error("_sfn_stop", "Unexpected error", error=str(e))
        err = str(e)
        # Ignore expected errors: already stopped, not found, wrong type
        if any(s in err for s in ('ExecutionDoesNotExist', 'InvalidArn', 'ABORTED', 'SUCCEEDED', 'FAILED')):
            return
        log.warn("_sfn_stop", "Could not stop execution", arn=arn[:80], context=context, error=err[:100])


def stop_execution(pipeline_execution: str, event: Dict) -> Dict:
    """Stop a pipeline execution — stops full hierarchy:
    acme-daily SFN → dependency_wrapper(s) → run_task(s) → DynamoDB.

    Accepts pipeline_execution (run ID) from query params.
    pipeline_name is resolved from DynamoDB task items.
    All stop calls are idempotent — safe to call multiple times.
    """
    if not pipeline_execution:
        return cors_response(400, {'error': 'pipeline_execution is required'})

    try:
        # Query all task items for this pipeline run
        # Include wrapper_execution_arn + task_execution_arn + pipeline_name
        items = executions_repo.query_by_pipeline_execution(
            pipeline_execution,
            projection='execution_name, #s, task_execution_arn, wrapper_execution_arn, pipeline_name',
            expr_names={'#s': 'status'},
        )

        # 1. Stop acme-daily SFN execution
        # Reconstruct from pipeline registry: sfn_arn + pipeline_execution name
        pipeline_name = next(
            (i.get('pipeline_name') for i in items if i.get('pipeline_name') and i.get('pipeline_name') != 'unknown'),
            None,
        )
        if pipeline_name:
            sfn_arn = pipelines_repo.get_sfn_arn(pipeline_name)
            if sfn_arn:
                pipeline_exec_arn = sfn_arn.replace(':stateMachine:', ':execution:') + ':' + pipeline_execution
                _sfn_stop(pipeline_exec_arn, context='pipeline')

        # 2. Stop all dependency_wrapper executions (deduplicated)
        wrapper_arns = {
            i['wrapper_execution_arn']
            for i in items
            if i.get('wrapper_execution_arn') and i['wrapper_execution_arn'].startswith('arn:')
        }
        for arn in wrapper_arns:
            _sfn_stop(arn, context='wrapper')

        # 3. Stop run_task executions + update DynamoDB
        _mark_pending_tasks_stopped(pipeline_execution, items=items)

        return cors_response(
            200,
            {
                'message': 'Execution stopped',
                'pipeline_execution': pipeline_execution,
                'wrappers_stopped': len(wrapper_arns),
            },
        )
    except Exception as e:
        log.error("stop_execution", "Error stopping execution", error=str(e))
        return cors_response(500, {'error': f'Failed to stop execution: {str(e)}'})


def _mark_pending_tasks_stopped(pipeline_execution: str, items: list = None) -> int:
    """Mark all non-terminal tasks as 'stopped' in DynamoDB and stop active run_task executions.

    Args:
        pipeline_execution: Run ID to query if items not provided
        items: Pre-fetched task items (avoids duplicate DynamoDB query when called from stop_execution)
    """
    stopped_at = datetime.now(timezone.utc).isoformat()
    updated_count = 0

    # Use pre-fetched items or query DynamoDB
    if items is None:
        items = executions_repo.query_by_pipeline_execution(
            pipeline_execution, projection='execution_name, #s, task_execution_arn', expr_names={'#s': 'status'}
        )

    for item in items:
        status = item.get('status', '')
        execution_name = item.get('execution_name', '')

        # Skip tasks already settled — terminal or deliberately stopped — plus
        # internal (_pause_) and Backfill records.
        if status in TASK_SETTLED_STATUSES or should_skip_token_row(item):
            continue

        # Stop active run_task SFN execution if present
        task_execution_arn = item.get('task_execution_arn', '')
        _sfn_stop(task_execution_arn, context='run_task')

        # Update to stopped
        try:
            executions_repo.update(
                execution_name,
                'SET #s = :status, finished_at = :finished',
                expr_values={
                    ':status': 'stopped',
                    ':finished': stopped_at,
                    ':s1': 'success',
                    ':s2': 'failed',
                    ':s3': 'skipped',
                    ':s4': 'stopped',
                    ':s5': 'aborted',
                    ':s6': 'upstream_failed',
                },
                expr_names={'#s': 'status'},
                condition_expr='attribute_exists(execution_name) AND NOT #s IN (:s1, :s2, :s3, :s4, :s5, :s6)',
            )
            updated_count += 1
        except executions_repo.conditional_check_exception:
            # Task already in terminal state, skip
            pass
        except Exception as e:
            log.error("executions", "Error updating task to stopped", error=str(e), execution_name=execution_name)

    log.info(
        "executions", "Marked tasks as stopped", updated_count=updated_count, pipeline_execution=pipeline_execution
    )
    return updated_count


def pause_execution(pipeline_execution: str, event: Dict) -> Dict:
    """
    Pause a pipeline execution. Running tasks will complete, but no new tasks will start.
    Tasks that would start will be marked as 'waiting_paused'.
    """
    try:
        # Create/update pause record
        pause_key = f"_pause_{pipeline_execution}"
        executions_repo.put(
            {
                'execution_name': pause_key,
                'paused': True,
                'paused_at': datetime.now(timezone.utc).isoformat(),
                'pipeline_execution': pipeline_execution,
            }
        )

        return cors_response(
            200, {'message': 'Pipeline execution paused', 'pipeline_execution': pipeline_execution, 'paused': True}
        )

    except Exception as e:
        log.error("pause_execution", "Error pausing execution", error=str(e))
        return cors_response(500, {'error': f'Failed to pause execution: {str(e)}'})


def get_paused_tasks_with_token(pipeline_execution: str) -> List[Dict]:
    """
    Get all tasks that are waiting_paused and have ANY token (pause_token OR wait_token).

    There are TWO types of paused tasks:
    1. pause_token: Task was ready to run but pipeline was paused (from pause_waiter)
    2. wait_token: Dependencies became ready but pipeline was paused (from notify_dependents)

    Both need to be resumed!
    """
    all_items = executions_repo.query_by_pipeline_execution(pipeline_execution)

    # Filter to paused tasks with EITHER pause_token OR wait_token
    return [
        item
        for item in all_items
        if item.get('status') == 'waiting_paused' and (item.get('pause_token') or item.get('wait_token'))
    ]


def resume_execution(pipeline_execution: str, event: Dict) -> Dict:
    """
    Resume a paused pipeline execution. Tasks in 'waiting_paused' will continue via callback.

    CRITICAL: Order of operations to avoid race condition:
    1. Get paused tasks WHILE pause flag is still True (atomic snapshot)
    2. Send callbacks to all waiting_paused tasks
    3. THEN update pause flag to False

    This prevents new tasks from seeing paused=False and starting without proper
    state recovery during the resume operation.

    IMPORTANT: There are TWO types of paused tasks:
    1. pause_token: Task was ready to run but pipeline was paused (pause_waiter)
    2. wait_token: Dependencies became ready but pipeline was paused (notify_dependents)

    Both need different handling!
    """
    pause_key = f"_pause_{pipeline_execution}"

    try:
        # 1. Get paused tasks BEFORE updating flag (while flag is still True)
        # This ensures we capture all tasks that were paused at this moment
        paused_tasks = get_paused_tasks_with_token(pipeline_execution)
        resumed_tasks = []
        failed_tasks = []

        # 2. Send callbacks FIRST - wake up all paused tasks
        for item in paused_tasks:
            task_name = item.get('task_name', '')
            execution_name = item.get('execution_name', '')
            pause_token = item.get('pause_token')
            wait_token = item.get('wait_token')
            pending_dep_keys = item.get('pending_dependency_keys', [])

            # Determine which token to use and appropriate output
            if pause_token:
                # Type 1: Task was ready to run but pipeline was paused
                token = pause_token
                output = {'resumed': True}
                token_type = 'pause_token'
            elif wait_token:
                # Type 2: Dependencies became ready but pipeline was paused
                token = wait_token
                output = {'signal': 'deps_ready', 'reason': 'resume'}
                token_type = 'wait_token'
            else:
                continue

            try:
                sfn.send_task_success(taskToken=token, output=json.dumps(output))

                # Clear token(s) and update status
                if token_type == 'pause_token':
                    executions_repo.update(
                        execution_name,
                        'REMOVE pause_token SET #s = :status',
                        expr_names={'#s': 'status'},
                        expr_values={':status': 'deps_ready'},
                    )
                else:
                    # For wait_token, also clear pending_dependency_keys
                    executions_repo.update(
                        execution_name,
                        'REMOVE wait_token, pending_dependency_keys SET #s = :status',
                        expr_names={'#s': 'status'},
                        expr_values={':status': 'deps_ready'},
                    )

                    # Cleanup dependency subscriptions (optional but recommended)
                    # This prevents stale subscriptions from accumulating
                    # NOTE: subscriber in subscriptions table = task_name (not execution_name)
                    if pending_dep_keys:
                        _cleanup_dependency_subscriptions(task_name, pending_dep_keys)

                resumed_tasks.append(f"{task_name} ({token_type})")

            except sfn.exceptions.TaskTimedOut:
                log.warn("resume_execution", "Token timed out", token_type=token_type, task_name=task_name)
                failed_tasks.append(f"{task_name} ({token_type} timeout)")
            except sfn.exceptions.InvalidToken:
                log.warn("resume_execution", "Invalid token", token_type=token_type, task_name=task_name)
                failed_tasks.append(f"{task_name} (invalid {token_type})")
            except Exception as e:
                log.error("resume_execution", "Failed to resume", error=str(e), task_name=task_name)
                failed_tasks.append(f"{task_name} ({str(e)[:50]})")

        # 3. NOW update pause flag to False (after all callbacks sent)
        executions_repo.update(
            pause_key,
            'SET paused = :p, resumed_at = :r',
            expr_values={':p': False, ':r': datetime.now(timezone.utc).isoformat()},
        )

        response_data = {
            'message': 'Pipeline execution resumed',
            'pipeline_execution': pipeline_execution,
            'paused': False,
            'resumed_tasks': resumed_tasks,
        }

        if failed_tasks:
            response_data['failed_tasks'] = failed_tasks

        return cors_response(200, response_data)

    except Exception as e:
        log.error("resume_execution", "Error resuming execution", error=str(e), pipeline_execution=pipeline_execution)
        return cors_response(500, {'error': f'Failed to resume execution: {str(e)}'})


def extend_pause(pipeline_execution: str, event: Dict) -> Dict:
    """
    Extend pause timeout by 12 hours. Sends heartbeat to all paused tasks.

    NOTE: Only sends heartbeat for tasks with pause_token (from pause_waiter).
    Tasks with only wait_token (from notify_dependents) cannot receive heartbeat
    because their Wait_For_Dependencies state has a fixed timeout.
    """
    try:
        paused_tasks = get_paused_tasks_with_token(pipeline_execution)
        extended_tasks = []
        skipped_wait_token = []

        for item in paused_tasks:
            task_name = item.get('task_name', '')
            pause_token = item.get('pause_token')

            # Skip tasks that only have wait_token (no heartbeat possible)
            if not pause_token:
                skipped_wait_token.append(task_name)
                continue

            try:
                sfn.send_task_heartbeat(taskToken=pause_token)
                extended_tasks.append(task_name)
            except sfn.exceptions.TaskTimedOut:
                log.warn("extend_pause", "Pause token timed out", task_name=task_name)
            except sfn.exceptions.InvalidToken:
                log.warn("extend_pause", "Invalid pause token", task_name=task_name)
            except Exception as e:
                log.error("extend_pause", "Failed to extend pause", error=str(e), task_name=task_name)

        response_data = {
            'message': 'Pause extended by 12 hours',
            'pipeline_execution': pipeline_execution,
            'extended_tasks': extended_tasks,
        }

        if skipped_wait_token:
            response_data['skipped_wait_token_tasks'] = skipped_wait_token
            response_data['warning'] = 'Tasks with wait_token have fixed 24h timeout and cannot be extended'

        return cors_response(200, response_data)

    except Exception as e:
        log.error("extend_pause", "Error extending pause", error=str(e))
        return cors_response(500, {'error': f'Failed to extend pause: {str(e)}'})


def get_execution_pause_status(pipeline_execution: str) -> bool:
    """Check if a pipeline execution is paused."""
    pause_key = f"_pause_{pipeline_execution}"

    try:
        item = executions_repo.get(pause_key, consistent=True)
        return (item or {}).get('paused', False)
    except Exception as e:
        # "Not paused" on read failure is the safe default — false negatives here
        # just mean a paused execution gets one extra poll, never an unintended
        # resume. Log so a real DDB outage doesn't silently disable pause UX.
        log.warn(
            "get_execution_pause_status",
            "Pause status read failed; defaulting to not paused",
            pipeline_execution=pipeline_execution,
            error=str(e),
        )
        return False

def register(router) -> None:
    """Register the free execution read routes. See ADR #97."""
    router.add('GET', '/api/runs', get_all_runs)
    router.add('GET', '/api/execution-children', get_execution_children, 'id')
    router.add('GET', '/api/execution-parent', get_execution_parent, 'id')
    # Execution control — free (ADR #110): intervene on a live run.
    router.add('POST', '/api/execution-stop', stop_execution, 'pipeline_execution')
    router.add('POST', '/api/execution-pause', pause_execution, 'id')
    router.add('POST', '/api/execution-resume', resume_execution, 'id')
    router.add('POST', '/api/execution-extend', extend_pause, 'id')
