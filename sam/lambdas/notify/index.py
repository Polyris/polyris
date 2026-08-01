"""
Notify Lambda (ADR #103).

Delivers failure alerts to a pipeline's enabled channels. Two input shapes:

1. **Batch** (preferred, used by failure_handler since the Stage-1 consolidation):
   the SFN invokes this Lambda once with just the pipeline + failure, and the
   Lambda reads alert_config from DynamoDB itself and loops the enabled channels
   with per-channel isolation. This keeps the config-read and the fan-out loop in
   one place (Python) instead of spread across SFN states.

   {
     "pipeline_name": "acme-daily",
     "failure": { "task_name": "...", "execution_name": "...", "error": "...",
                  "date": "...", "links": [...] }
   }
   ->
   { "delivered": true, "channels": [ {channel, delivered, status}, ... ] }

2. **Single channel** (legacy / direct): deliver one channel given its config.
   Kept so a caller can still target exactly one channel.

   { "channel": "slack", "config": {...}, "failure": {...} }
   ->
   { "channel": "slack", "delivered": true, "status": 200 }

Delivery never raises out of the handler: a channel failing is captured in the
result, not propagated, so one bad channel never blocks the others.
"""

from typing import Any, Dict, List

from actions import dispatch_action
from logger import log
from notifiers import get_notifier
from registry import get_alert_config


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Deliver alert(s). Catches everything - a failure is data in the result,
    never an exception, so the SFN step always succeeds.

    Input shapes (checked in order):
    - ``action``: a Stage-2 directed action — interactive Slack post, live
      PagerDuty alert, or PagerDuty resolve. These move the HTTP+SSM posting that
      used to live in dedicated helper SFNs into this Lambda (ADR #103 Stage 2).
    - ``pipeline_name`` (no ``channel``): batch — read alert_config + fan out.
    - ``channel``: single — deliver exactly one channel.
    """
    try:
        action = event.get('action')
        if action:
            return _handle_action(action, event)
        # Batch shape has pipeline_name and no explicit channel.
        if 'channel' not in event and event.get('pipeline_name'):
            return _handle_batch(event)
        return _handle_single(event)
    except Exception as e:
        log.error('handler', 'Unhandled error',
                  error_type=type(e).__name__, error=str(e), event=event)
        return {'delivered': False, 'reason': 'unhandled_error'}


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
    pipeline_name = event.get('pipeline_name', '')
    failure = event.get('failure') or {}
    failure.setdefault('pipeline_name', pipeline_name)

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
    """Deliver exactly one channel given its config (legacy/direct shape)."""
    channel = event.get('channel', '')
    config = event.get('config') or {}
    failure = event.get('failure') or {}

    if not channel:
        log.warn('notify', 'No channel in event; skipping')
        return {'channel': 'unknown', 'delivered': False, 'reason': 'no_channel'}

    return _deliver_one(channel, config, failure)


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
