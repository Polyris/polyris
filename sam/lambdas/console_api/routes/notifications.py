"""Notifications routes for Console API: the bell.

The bell reads the attention index only (ADR #137 §6, D92): every task that failed
within its window, by when it failed, and every open decision request, whatever the
date of its run; with the coordination warnings of its window (ADR #130 §9) and the
backfills that ended in it (ADR #68). It keeps one notification per run, a decision
request over a failure, and names each by its event: the UI keeps "read" and
"cleared" by that id. A run from before 1.3 has no entry of these kinds, so the
bell shows none of its failures.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

from dal import executions_repo, backfills_repo
from constants import Limits
from response import cors_response
from logger import log
from utils import safe_param_int
from polyris.coordination import attention, deadlines
from polyris.coordination.times import format_utc


def get_notifications(event: Dict) -> Dict:
    """Recent failures, open decision requests, warnings and ended backfills, for the bell.

    Args:
        event: API Gateway event with optional query parameters:
            - limit: Maximum notifications to return (default: 10, max: 50)
            - hours: Time window in hours (default: 24, max: 168)

    Returns:
        CORS response with notifications list and count. The open decision
        requests come first, the most urgent first; then the rest, newest first.
    """
    params = event.get('queryStringParameters', {}) or {}
    limit = safe_param_int(params, 'limit', 10, 50)
    hours_back = safe_param_int(params, 'hours', 24, Limits.MAX_NOTIFICATION_HOURS)

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours_back)

    try:
        runs: Dict[str, Dict] = {}  # one notification per run
        for entry in executions_repo.attention_entries(attention.FAILED, since=format_utc(cutoff)):
            runs.setdefault(_run_key(entry), _failure(entry, now))
        for entry in executions_repo.attention_entries(attention.DECISION):
            runs[_run_key(entry)] = _decision(entry)        # a request for a decision outranks a failure
        decisions = sorted((n for n in runs.values() if n['type'] == 'decision_required'),
                           key=lambda n: n['deadline'] or '~')    # the most urgent first; a paused one last
        others = [n for n in runs.values() if n['type'] != 'decision_required']
        others += _coordination_warnings(cutoff, now, limit)
        try:
            others += _backfill_terminal_notifications(cutoff.isoformat(), now)
        except (KeyError, ValueError, TypeError) as e:
            log.warn("notifications", "Failed to fetch backfill notifications", error=str(e))
        others.sort(key=lambda x: x.get('finished_at', ''), reverse=True)
        notifications = (decisions + others)[:limit]

        return cors_response(200, {
            'notifications': notifications,
            'count': len(notifications)
        })

    except Exception as e:
        log.error("notifications", "Error in get_notifications", error=str(e))
        return cors_response(500, {'error': f'Failed to get notifications: {str(e)}'})


def _run_key(entry: Dict) -> str:
    return f"{entry.get('pipeline_name', '')}:{entry.get('pipeline_execution', '')}"


def _event_id(kind: str, entry: Dict) -> str:
    """A notification's id names its event, not its run: a run of 1.3 goes on after a failure and may ask for
    several decisions, and the bell keeps "read" by id (review of TR-6d, B-2). The task row and the time its entry
    falls due tell one event from the next, a decision after a Restart included. A pause or a resume of the run
    moves a decision's deadline, and with it the id: the request shows again, with the deadline it now has."""
    return f"{kind}:{entry.get('execution_name', '')}:{entry.get(attention.DUE, '')}"


def _failure(entry: Dict, now: datetime) -> Dict:
    """A task that failed: its entry falls due when it failed (ADR #137 §6)."""
    failed_at = str(entry.get(attention.DUE, ''))
    return {
        'id': _event_id('failure', entry),
        'type': 'failure',
        'pipeline_name': entry.get('pipeline_name', ''),
        'task_name': entry.get('task_name', ''),
        'pipeline_execution': entry.get('pipeline_execution', ''),
        'error': entry.get('error', 'Unknown error'),
        'date': entry.get('task_date', ''),
        'finished_at': failed_at,
        'time_ago': _format_time_ago(failed_at, now) if failed_at else 'just now',
    }


def _decision(entry: Dict) -> Dict:
    """A task that waits for an operator's decision: its entry falls due at the deadline, far in the future while
    its run is paused, when the bell shows no deadline (ADR #137 §5, §6)."""
    return {
        'id': _event_id('decision', entry),
        'type': 'decision_required',
        'pipeline_name': entry.get('pipeline_name', ''),
        'task_name': entry.get('task_name', ''),
        'pipeline_execution': entry.get('pipeline_execution', ''),
        'error': '',
        'date': entry.get('task_date', ''),
        'deadline': _deadline(str(entry.get(attention.DUE, ''))),
        'finished_at': '',
        'time_ago': '',
    }


def _deadline(due: str) -> Optional[str]:
    """The decision's deadline, or None while a pause holds it or it cannot be read."""
    try:
        return deadlines.shown(due)
    except ValueError:
        log.warn("notifications", "Unreadable decision deadline", attention_at=due)
        return None


def _coordination_warnings(cutoff: datetime, now: datetime, limit: int) -> list:
    """The bell's warning rows; a failed read is logged and leaves the rest of the bell intact."""
    try:
        rows = executions_repo.recent_warnings(format_utc(cutoff), limit)
    except (ClientError, BotoCoreError) as e:
        log.error("notifications", "Could not read coordination warnings", error=str(e))
        return []
    return [{
        'id': str(row.get('execution_name', '')),
        'type': 'warning',
        'kind': row.get('kind', ''),
        'pipeline_name': row.get('pipeline_name', ''),
        'task_name': row.get('task_name', ''),
        'pipeline_execution': row.get('pipeline_execution', ''),
        'error': row.get('error', ''),
        'date': row.get('task_date', ''),
        'finished_at': row.get('finished_at', ''),
        'time_ago': _format_time_ago(str(row['finished_at']), now) if row.get('finished_at') else 'just now',
    } for row in rows]


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
