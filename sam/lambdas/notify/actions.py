"""
Directed-action notifiers for the notify Lambda (ADR #103 Stage 2).

Beyond the per-channel failure alerts (notifiers.py), the Lambda also handles a
few *directed* actions that used to live in dedicated helper Step Functions:
- ``interactive_slack`` — post the failure message **with action buttons**
  (skip / restart / fail / success) so on-call can decide during the wait window.
- ``live_pagerduty`` — trigger a PagerDuty incident the moment a task fails but is
  still live (the 5h window), so on-call can intervene before it goes terminal.
- ``resolve_pagerduty`` — resolve that incident when the task later succeeds/skips.

These are all HTTP + SSM posting, which is exactly what this Lambda does — so
Stage 2 moves them here and deletes the helper SFNs (Stage 3). The waiting
(waitForTaskToken, the 5h Wait) stays in the SFN; only the *posting* moves.

Like channels, the action notifiers are paid (Slack/PagerDuty) and registered by
the ee package. The OSS build has no ``notify/ee/`` → the actions are absent →
an unknown action no-ops (logged), never crashes. Same physical-strip seam as
notifiers.py (РОЗЧЕПЛЕННЯ, ADR #98/#102).
"""

from typing import Any, Callable, Dict

from logger import log

# action name → callable(event) -> result dict. Free build is empty; the ee
# package fills it via register_action (ADR #97 explicit registration).
ACTION_NOTIFIERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def register_action(action: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
    """Register a directed-action handler. Used by the ee package to add the paid
    actions (interactive Slack, live/resolve PagerDuty) without editing this file."""
    ACTION_NOTIFIERS[action] = fn


def dispatch_action(action: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Run a directed action; unknown action is a logged no-op, never a crash."""
    fn = ACTION_NOTIFIERS.get(action)
    if fn is None:
        log.warn('actions', 'Unknown action; skipping', action=action)
        return {'action': action, 'delivered': False, 'reason': 'unknown_action'}
    return fn(event)
