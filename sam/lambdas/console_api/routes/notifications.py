"""Notifications routes for Console API.

Handles endpoints related to failure notifications and alerts.

v0.78.11: extended to also emit backfill terminal events
(completed/failed/partial/canceled) finished within the time window.
See ADR #68.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict

from boto3.dynamodb.conditions import Key, Attr

from dal import executions_repo, backfills_repo
from constants import Limits
from response import cors_response
from logger import log
from utils import safe_param_int


def get_notifications(event: Dict) -> Dict:
    """Get recent failures and alerts for notification display.
    
    Optimized to use GSI 'date-pipeline-index' instead of full table scan.
    Queries recent dates and filters for failed status.
    
    Args:
        event: API Gateway event with optional query parameters:
            - limit: Maximum notifications to return (default: 10, max: 50)
            - hours: Time window in hours (default: 24, max: 168)
    
    Returns:
        CORS response with notifications list and count.
    """
    params = event.get('queryStringParameters', {}) or {}
    limit = safe_param_int(params, 'limit', 10, 50)
    hours_back = safe_param_int(params, 'hours', 24, Limits.MAX_NOTIFICATION_HOURS)
    
    # Calculate time window
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_back)
    cutoff_iso = cutoff.isoformat()
    
    # Generate list of dates to query (more efficient than full scan)
    dates_to_query = []
    current_date = now
    for _ in range(min(hours_back // 24 + 1, 8)):  # Max 8 days
        dates_to_query.append(current_date.strftime('%Y-%m-%d'))
        current_date -= timedelta(days=1)
    
    notifications = []
    run_state: Dict[str, Dict] = {}  # dedup_key -> one notification per run
    items_scanned = 0
    
    try:
        # Query each date using GSI (O(log n) per date instead of full scan)
        for date_str in dates_to_query:
            if items_scanned >= Limits.MAX_NOTIFICATIONS_SCAN:
                break
                
            last_key = None
            while items_scanned < Limits.MAX_NOTIFICATIONS_SCAN:
                query_params = {
                    'KeyConditionExpression': Key('date').eq(date_str),
                    # Two attention states (ADR #107): a task that failed, or one paused
                    # awaiting a manual decision (skip / mark success / fail).
                    'FilterExpression': Attr('status').is_in(['failed', 'waiting_decision']),
                    'ProjectionExpression': 'pipeline_name, task_name, pipeline_execution, finished_at, started_at, running_at, #s, #e, #d',
                    'ExpressionAttributeNames': {'#s': 'status', '#e': 'error', '#d': 'date'}
                }
                if last_key:
                    query_params['ExclusiveStartKey'] = last_key
                
                response = executions_repo.query_by_date_raw(**query_params)
                items_scanned += response.get('Count', 0)
                
                for item in response.get('Items', []):
                    status = item.get('status', '')
                    is_decision = status == 'waiting_decision'
                    # A waiting_decision task is paused (not finished) so it has no
                    # finished_at — anchor it on when it started running/waiting, and
                    # always surface it (it is actively blocking the run right now).
                    ts = item.get('finished_at') or item.get('running_at') or item.get('started_at') or ''
                    if not is_decision and (not ts or ts < cutoff_iso):
                        continue  # a plain failure must fall within the time window

                    pipeline_name = item.get('pipeline_name', '')
                    exec_id = item.get('pipeline_execution', '')
                    dedup_key = f"{pipeline_name}:{exec_id}"

                    # One notification per run; a decision-required state outranks a plain
                    # failure (it needs human action), so it wins when both are present.
                    existing = run_state.get(dedup_key)
                    if existing and (existing['type'] == 'decision_required' or not is_decision):
                        continue

                    run_state[dedup_key] = {
                        'id': dedup_key,
                        'type': 'decision_required' if is_decision else 'failure',
                        'pipeline_name': pipeline_name,
                        'task_name': item.get('task_name', ''),
                        'pipeline_execution': exec_id,
                        'error': '' if is_decision else item.get('error', 'Unknown error'),
                        'date': item.get('date', ''),
                        'finished_at': ts,
                        'time_ago': _format_time_ago(ts, now) if ts else 'just now'
                    }
                
                last_key = response.get('LastEvaluatedKey')
                if not last_key:
                    break
        
        # Sort by timestamp descending (newest first)
        notifications = list(run_state.values())
        notifications.sort(key=lambda x: x.get('finished_at', ''), reverse=True)
        notifications = notifications[:limit]

        # Append backfill terminal events (ADR #68). Backfills are infrequent
        # (a few per day at most) so list_recent is fine; we filter to terminal
        # statuses finished within the same time window.
        try:
            backfill_notifs = _backfill_terminal_notifications(cutoff_iso, now)
            notifications.extend(backfill_notifs)
            # Re-sort and re-cap
            notifications.sort(key=lambda x: x.get('finished_at', ''), reverse=True)
            notifications = notifications[:limit]
        except (KeyError, ValueError, TypeError) as e:
            log.warn("notifications", "Failed to fetch backfill notifications",
                     error=str(e))

        return cors_response(200, {
            'notifications': notifications,
            'count': len(notifications)
        })
        
    except Exception as e:
        log.error("notifications", "Error in get_notifications", error=str(e))
        return cors_response(500, {'error': f'Failed to get notifications: {str(e)}'})


_BACKFILL_TERMINAL_STATUSES = {'completed', 'failed', 'partial', 'canceled'}


def _backfill_terminal_notifications(cutoff_iso: str, now: datetime) -> list:
    """Fetch backfills that reached a terminal state within the time window.

    Returns notification entries with type='backfill' and a status-specific
    subtype so the UI can render appropriate icons/colors. Each entry is a
    dict shaped like the existing failure notifications, with backfill-specific
    fields (backfill_id, target_pipeline, partition counts).
    """
    notifs = []
    items = backfills_repo.list_recent(limit=100)
    for item in items:
        status = item.get('status')
        if status not in _BACKFILL_TERMINAL_STATUSES:
            continue
        finished_at = item.get('finished_at', '')
        if not finished_at or finished_at < cutoff_iso:
            continue
        backfill_id = item.get('backfill_id') or item.get('execution_name')
        notifs.append({
            'id': f'backfill:{backfill_id}',
            'type': 'backfill',
            'backfill_status': status,
            'backfill_id': backfill_id,
            'target_pipeline': item.get('target_pipeline', ''),
            'total_partitions': int(item.get('total_partitions', 0) or 0),
            'completed_partitions': int(item.get('completed_partitions', 0) or 0),
            'failed_partitions': int(item.get('failed_partitions', 0) or 0),
            'finished_at': finished_at,
            'time_ago': _format_time_ago(finished_at, now),
        })
    return notifs


def _format_time_ago(finished_at: str, now: datetime) -> str:
    """Format finished_at as relative time string."""
    try:
        finished_dt = datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
        delta = now - finished_dt
        if delta.total_seconds() < 60:
            return 'just now'
        elif delta.total_seconds() < 3600:
            mins = int(delta.total_seconds() / 60)
            return f'{mins}m ago'
        elif delta.total_seconds() < 86400:
            hours = int(delta.total_seconds() / 3600)
            return f'{hours}h ago'
        else:
            days = int(delta.total_seconds() / 86400)
            return f'{days}d ago'
    except (ValueError, TypeError, AttributeError):
        return ''


def register(router) -> None:
    """Register notification routes. See ADR #97."""
    router.add('GET', '/api/notifications', get_notifications)
