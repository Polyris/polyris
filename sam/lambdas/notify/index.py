"""
Notify Lambda (ADR #103).

Delivers failure alerts to a pipeline's enabled channels. Four input shapes:

0. **Action** (``action`` set): what ``run_task`` and the console ask for — an
   interactive Slack post, a live PagerDuty alert, or the resolve of one
   (``actions.py``).

1. **Batch** (preferred for a delivery to every channel): the caller invokes
   this Lambda once with just the pipeline + failure, and the
   Lambda reads alert_config from DynamoDB itself and loops the enabled channels
   with per-channel isolation. This keeps the config-read and the fan-out loop in
   one place (Python) instead of spread across SFN states.

   {
     "pipeline_name": "acme-daily",
     "failure": { "execution_name": "...", "error": "..." }
   }
   ->
   { "delivered": true, "channels": [ {channel, delivered, status}, ... ] }

2. **Single channel**: deliver one of the pipeline's enabled channels.

   { "pipeline_name": "acme-daily", "channel": "webhook", "failure": {...} }
   ->
   { "channel": "webhook", "delivered": true, "status": 200 }

3. **Test**: ``{"pipeline_name": "acme-daily", "channel": "slack", "test": true}``
   delivers a test message through one of the pipeline's saved and enabled channels.

A pipeline's roles may invoke this function (ADR #142 §5), so nothing that points
an alert, a link or a button comes from the event:
- channels and their config come from the registry's alert_config, the secrets
  from SSM; a ``config`` in the event is ignored;
- the failure is rebuilt from the task row its ``execution_name`` names, which
  must be a row of the event's pipeline: its task, date and execution come from
  the row, and only the error text from the event, as a failed task's own error
  is its author's anyway. ``links`` and any other field of the event's failure
  are dropped. An event whose execution is no row of its pipeline is refused.

Delivery never raises out of the handler: a channel failing is captured in the
result, not propagated, so one bad channel never blocks the others.
"""

from typing import Any, Dict, List, Optional, Tuple

from actions import dispatch_action
from logger import log
from notifiers import get_notifier
from registry import get_alert_config, get_task_row

#: What a test alert says: the task it names, and its error text.
TEST_TASK = 'test-alert'
TEST_ERROR = 'This is a test alert from Polyris. If you can see this, the channel works.'
#: The longest error text an alert carries.
ERROR_LIMIT = 2000


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Deliver alert(s). Catches everything - a failure is data in the result,
    never an exception, so the SFN step always succeeds.

    Input shapes (checked in order):
    - ``action``: a Stage-2 directed action — interactive Slack post, live
      PagerDuty alert, or PagerDuty resolve. These move the HTTP+SSM posting that
      used to live in dedicated helper SFNs into this Lambda (ADR #103 Stage 2).
    - ``pipeline_name`` (no ``channel``): batch — read alert_config + fan out.
    - ``channel``: single — deliver exactly one of the pipeline's enabled channels.
    """
    try:
        action = event.get('action')
        if not action and 'channel' in event and not event.get('channel'):
            log.warn('notify', 'No channel in event; skipping')
            return {'channel': 'unknown', 'delivered': False, 'reason': 'no_channel'}
        failure, refusal = platform_failure(event, test_allowed=not action)
        if failure is None:
            key = {'action': action} if action else {'channel': event.get('channel', '')}
            return {**key, 'delivered': False, 'reason': refusal}
        event = {**event, 'failure': failure}
        if action:
            return _handle_action(action, event)
        # Batch shape has pipeline_name and no explicit channel.
        if 'channel' not in event:
            return _handle_batch(event)
        return _handle_single(event)
    except Exception as e:
        log.error('handler', 'Unhandled error',
                  error_type=type(e).__name__, error=str(e), event=event)
        return {'delivered': False, 'reason': 'unhandled_error'}


def platform_failure(event: Dict[str, Any], *, test_allowed: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    """The failure to deliver, from the platform's rows, or None and the reason it is refused.

    The event names a pipeline and the execution of a failed task; the task row must exist and be the
    pipeline's. A test (``test``, for a channel, not an action) names no execution.
    """
    pipeline_name = event.get('pipeline_name')
    if not isinstance(pipeline_name, str) or not pipeline_name:
        log.warn('notify', 'No pipeline in event; skipping')
        return None, 'no_pipeline'
    if event.get('test') is True:
        if not test_allowed:
            return None, 'not_a_test_action'
        return {'pipeline_name': pipeline_name, 'task_name': TEST_TASK, 'execution_name': '', 'date': '',
                'error': TEST_ERROR}, ''
    claimed = event.get('failure') if isinstance(event.get('failure'), dict) else {}
    execution_name = claimed.get('execution_name')
    if not isinstance(execution_name, str) or not execution_name or execution_name.startswith('_'):
        log.warn('notify', 'The event names no task execution; skipping', pipeline=pipeline_name)
        return None, 'no_execution'
    try:
        row = get_task_row(execution_name)
    except Exception as e:
        # Principle #38: an alert that cannot be checked is not sent, and the log says why.
        log.error('notify', 'Could not read the task row the alert names; nothing sent',
                  pipeline=pipeline_name, execution_name=execution_name, error_type=type(e).__name__, error=str(e))
        return None, 'row_unreadable'
    if not row.get('task_name') or row.get('pipeline_name') != pipeline_name:
        log.warn('notify', 'The execution is no task row of the pipeline; nothing sent',
                 pipeline=pipeline_name, execution_name=execution_name)
        return None, 'unknown_execution'
    error = claimed.get('error')
    return {'pipeline_name': pipeline_name, 'task_name': str(row['task_name']), 'execution_name': execution_name,
            'date': str(row.get('date') or ''),
            'error': str(error)[:ERROR_LIMIT] if error is not None else ''}, ''


def _handle_action(action: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a Stage-2 directed action to its notifier. Unknown action is a
    no-op (logged), never a crash. The action notifiers (interactive Slack, live
    PagerDuty, resolve) are paid and registered by the ee package; in the OSS
    build they are absent and the action no-ops — same contract as channels."""
    try:
        return dispatch_action(action, event)
    except Exception as e:
        log.error('handler', 'Action delivery failed',
                  action=action, error_type=type(e).__name__, error=str(e))
        return {'action': action, 'delivered': False, 'reason': 'delivery_error'}


def _handle_batch(event: Dict[str, Any]) -> Dict[str, Any]:
    """Read the pipeline's config and deliver to each enabled channel, isolating
    per-channel failures."""
    pipeline_name = event['pipeline_name']
    failure = event['failure']

    cfg = get_alert_config(pipeline_name)
    enabled: List[str] = cfg.get('enabled_channels') or []
    if not enabled:
        # OSS / not configured / read failed -> clean no-op.
        return {'delivered': True, 'channels': []}

    results = []
    any_delivered = False
    for channel in enabled:
        channel_config = cfg.get(channel) or {}
        result = _deliver_one(channel, channel_config, failure)
        results.append(result)
        any_delivered = any_delivered or bool(result.get('delivered'))

    return {'delivered': any_delivered, 'channels': results}


def _handle_single(event: Dict[str, Any]) -> Dict[str, Any]:
    """Deliver exactly one of the pipeline's enabled channels, with its config from the registry."""
    channel = event['channel']
    pipeline_name = event['pipeline_name']
    cfg = get_alert_config(pipeline_name)
    if channel not in (cfg.get('enabled_channels') or []):
        log.warn('notify', 'Channel not enabled for the pipeline; skipping', channel=channel, pipeline=pipeline_name)
        return {'channel': channel, 'delivered': False, 'reason': 'not_enabled'}
    return _deliver_one(channel, cfg.get(channel) or {}, event['failure'])


def _deliver_one(channel: str, config: Dict[str, Any], failure: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve the notifier for one channel and deliver, never raising."""
    try:
        notifier = get_notifier(channel, config)
        if notifier is None:
            return {'channel': channel, 'delivered': False, 'reason': 'unknown_channel'}
        return notifier.notify(failure)
    except Exception as e:
        log.error('notify', 'Channel delivery failed',
                  channel=channel, error_type=type(e).__name__, error=str(e))
        return {'channel': channel, 'delivered': False, 'reason': 'delivery_error'}
