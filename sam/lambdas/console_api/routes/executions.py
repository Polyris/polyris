"""Execution routes for Console API.

Handles pipeline execution control operations:
- get_all_runs: The Runs tab: run rows newest first, with the backfills
- stop_execution: Stop a run: its stop mark, which the dispatcher carries out (ADR #137 §4)
- pause_execution: Pause a run: tasks that run go on, none starts (ADR #137 §5)
- resume_execution: Resume a paused run
- get_execution_children: Get child tasks of an execution
- get_execution_parent: Get parent execution info
"""
import json
import random
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from botocore.exceptions import ClientError, BotoCoreError

from polyris.coordination import actions, run_end, run_row
from routes.tasks import ACTION_BACKOFF_SECONDS
from dal import executions_repo, backfills_repo
from constants import Limits
from feed import duration_ms, page_by_started_at, run_entry, run_rows_before, runs_of_date
from response import cors_response, index_building_response, safe_parse_body
from logger import log
from auth import operator_display
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

    Backfills live in pipeline-tokens under a sentinel ``pipeline_name``; no run
    list reads them (they are no run rows, and ``should_skip_token_row`` drops them
    from a date's task rows), so they are merged into ``/api/runs`` here as
    first-class rows. Status stays in the 6-state Backfill vocabulary (ADR #56)
    — no normalization to execution statuses; the ``kind`` field lets the UI
    interpret it. Children are not embedded: a backfill row expands on demand
    via ``GET /api/backfills/by-id``.

    ``before`` is the feed's paging cursor (see ``feed.py``) and is handed to the repo
    rather than applied here: ``list_recent`` sorts newest-first, so a cursor applied
    after a ``limit`` slice would return nothing on every page but the first, and
    Backfills would quietly vanish from the feed as soon as you paged.

    **Filters run before the page is cut, and the repo is asked for no limit at all.**
    ``list_recent`` with a ``limit`` stops once it has that many records, before any
    filter here has run, so slicing first just spends the page on records the filters
    are about to drop. ``?status=completed`` with a run of
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
            'duration_ms': duration_ms(started_at, finished_at),
        })
    return rows


def get_all_runs(event: Dict) -> Dict:
    """The Runs tab: every pipeline's runs, newest first, with the backfills (ADR #95).

    Without a date it reads run rows from run-start-index (ADR #137 §5): the start days
    of the last ``Limits.RUNS_WINDOW_DAYS`` days, newest first, stopping once the page is
    full, with a pipeline filter or without. Only runs started on 1.3 have run rows. A
    date filter finds its runs through the task rows of that date, with no window, runs
    from before 1.3 included (``feed.runs_of_date``). Each run's status comes from its
    run row's summary; the console asks Step Functions nothing about a run (§4).

    Pages with an opaque ``before`` cursor and answers with ``next`` (see ``feed.py``).
    Backfills page through the same cursor: they carry ``started_at`` too, which is
    what makes one cursor enough for the merged feed (ADR #95).
    """
    params = event.get('queryStringParameters', {}) or {}
    limit = safe_param_int(params, 'limit', Limits.RUNS_FEED_LIMIT, 200)
    before = params.get('before', '')
    date_filter = params.get('date', '')
    pipeline_filter = params.get('pipeline', '')
    status_filter = params.get('status', '')

    def keeps(run: Dict) -> bool:
        return not status_filter or run['status'] == status_filter

    try:
        more = False                    # whether the walk of run rows stopped before the window's end
        if status_filter and status_filter not in run_end.SHOWN_STATUSES:
            runs = []                   # a backfill's status (ADR #95): no run has it, so no run row is read
        elif date_filter:
            runs = [r for r in runs_of_date(date_filter, pipeline_filter) if keeps(r)]
        else:
            # The status is derived from each run row, so it filters here, and the walk counts only what it keeps.
            runs, more = run_rows_before(before, limit + 1, days=Limits.RUNS_WINDOW_DAYS, pipeline=pipeline_filter,
                                         keep=lambda item: keeps(run_entry(item)))
            runs = [run_entry(item) for item in runs]
        all_runs = [{'kind': 'execution', **run} for run in runs]

        # Merge Backfills as first-class 'backfill' rows (ADR #95). Backfill records are no run rows and no task
        # rows, so there is no double-count: they enter only here.
        all_runs.extend(
            _build_backfill_run_rows(limit, status_filter, pipeline_filter, date_filter, before)
        )

        # One page, newest first, plus the cursor for the older one. Both kinds sort
        # and page on the same started_at, so the merge needs no second cursor.
        page, next_cursor = page_by_started_at(all_runs, before, limit, more=more)

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
    except executions_repo.IndexBuilding:
        return index_building_response()
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
        
        # Sort by started_at, then the task's name: every row of a 1.3 run carries its run's one
        # started_at (Start_Run), so the name keeps the order the same from one read to the next.
        # A row without one sorts first instead of failing the sort, which the catch below
        # turned into an empty list for every run.
        children.sort(key=lambda x: (x.get('started_at') or '', x.get('task_name') or ''))
        
        return cors_response(200, {
            'pipeline_execution': execution_name,
            'children': children,
            'count': len(children)
        })
    
    except Exception as e:
        # An empty list here once hid, for every run, a row the sort could not compare.
        log.error("get_execution_children", "Error getting children", error=str(e), execution_name=execution_name)
        return cors_response(500, {'error': f'Failed to read the run\'s tasks: {e}'})


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



#: A run action whose write a change of the run row refused is built again from a new read, this many times in all.
RUN_ACTION_ATTEMPTS = 3


def stop_execution(pipeline_execution: str, event: Dict) -> Dict:
    """Stop a run (ADR #137 §4): its stop mark, with who asked and why (``reason`` in the body).

    The dispatcher carries the stop out: it stops the run's unfinished rows, dependants first, then the run's
    execution and any run_task still running, and answers the run. Safe to repeat: a second Stop changes nothing.
    """
    body, err = safe_parse_body(event)
    if err:
        return err
    operator, reason = operator_display(event), str(body.get('reason') or 'Stopped via UI')
    return _run_action('stop', pipeline_execution, lambda run, now: actions.stop_run(
        run, by=operator, reason=reason, now=now, table=executions_repo.table.name),
        {'message': 'Stopping the run: its tasks stop, dependants first', 'stopping': True})


def pause_execution(pipeline_execution: str, event: Dict) -> Dict:
    """Pause a run (ADR #137 §5): tasks that run go on, none starts until the Resume; a pause has no time limit."""
    return _run_action('pause', pipeline_execution, lambda run, now: actions.pause_run(
        run, now=now, table=executions_repo.table.name), {'message': 'Pipeline execution paused', 'paused': True})


def resume_execution(pipeline_execution: str, event: Dict) -> Dict:
    """Resume a paused run (ADR #137 §5): the dispatcher decides the rows the pause held, from the run row's change."""
    return _run_action('resume', pipeline_execution, lambda run, now: actions.resume_run(
        run, now=now, table=executions_repo.table.name), {'message': 'Pipeline execution resumed', 'paused': False})


def _run_action(action: str, pipeline_execution: str, build: Callable[[Dict, datetime], Optional[Dict]],
                answer: Dict) -> Dict:
    """Read the run row, build the action's write from it and send it, built again from a new read while a change
    of the run row refuses it. A write the builder finds needless (a second Stop, a Pause of a paused run) is no
    error: the run already is as asked."""
    if not pipeline_execution:
        return cors_response(400, {'error': 'pipeline_execution is required'})
    for _ in range(RUN_ACTION_ATTEMPTS):
        run = executions_repo.get(run_row.key(pipeline_execution), consistent=True)
        if run is None:
            return cors_response(409, {'error': f'{pipeline_execution}: no run row; a run from before 1.3 takes no '
                                                f'{action}, and so does no run at all',
                                       'pipeline_execution': pipeline_execution})
        try:
            update = build(run, datetime.now(timezone.utc))
        except actions.ActionRefused as exc:
            return cors_response(409, {'error': str(exc), 'pipeline_execution': pipeline_execution})
        if update is None:
            break
        try:
            executions_repo.transact_write([{'Update': update}])
            break
        except executions_repo.transaction_canceled_exception:
            log.info(action, "The run row changed under the action: built again from a new read",
                     pipeline_execution=pipeline_execution)
            time.sleep(random.uniform(*ACTION_BACKOFF_SECONDS))
    else:
        return cors_response(409, {'error': f'{pipeline_execution} kept changing under the {action}; try again',
                                   'pipeline_execution': pipeline_execution})
    log.info(action, "Done", pipeline_execution=pipeline_execution)
    return cors_response(200, {**answer, 'pipeline_execution': pipeline_execution})


def register(router) -> None:
    """Register the free execution read routes. See ADR #97."""
    router.add('GET', '/api/runs', get_all_runs)
    router.add('GET', '/api/execution-children', get_execution_children, 'id')
    router.add('GET', '/api/execution-parent', get_execution_parent, 'id')
    # Execution control — free (ADR #110): intervene on a live run.
    router.add('POST', '/api/execution-stop', stop_execution, 'pipeline_execution')
    router.add('POST', '/api/execution-pause', pause_execution, 'id')
    router.add('POST', '/api/execution-resume', resume_execution, 'id')
