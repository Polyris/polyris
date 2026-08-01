"""
Notifier registry for the notify Lambda (ADR #103).

Each channel is a Notifier subclass that formats and delivers one kind of alert.
The registry is built by explicit registration (ADR #97): the base channels
register here, and — if the proprietary `ee` package ships in this build — its
register() adds the paid channels (Slack, PagerDuty). The free↔paid seam is
physical (ADR #98/#102, РОЗЧЕПЛЕННЯ): the OSS build has no `notify/ee/`, so the
import fails and only the free channels are present. `get_notifier` then no-ops
on an unknown channel rather than crashing.

Secrets (Slack webhook URL, PagerDuty routing key) are NOT passed in the event.
The event carries the SSM parameter name; the notifier fetches the secret from
SSM (SecureString, WithDecryption) at delivery time. See ADR #103 "Where values
are stored".
"""

import json
import os
import urllib.request
from typing import Any, Dict, Optional

import boto3
from logger import log

_ssm = None


def _ssm_client():
    """Lazy SSM client (so unit tests can patch boto3 without a live client)."""
    global _ssm
    if _ssm is None:
        _ssm = boto3.client('ssm', region_name=os.environ.get('AWS_REGION', 'us-east-1'))
    return _ssm


def _get_secret(param_name: str) -> Optional[str]:
    """Read a SecureString from SSM Parameter Store. Returns None on any failure
    (a missing/locked secret must not crash the whole fan-out — the channel just
    doesn't deliver)."""
    if not param_name:
        return None
    try:
        resp = _ssm_client().get_parameter(Name=param_name, WithDecryption=True)
        return resp['Parameter']['Value']
    except Exception as e:
        log.error('get_secret', 'Failed to read SSM parameter',
                  param=param_name, error_type=type(e).__name__, error=str(e))
        return None


def _http_post_json(url: str, payload: dict, timeout: int = 5) -> int:
    """POST JSON, return HTTP status. Raises on transport error (caller logs)."""
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'},
                                 method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


class Notifier:
    """Base class. A notifier formats a failure into a channel-specific payload
    and delivers it. `config` is the per-channel dict from alert_config;
    `failure` is the failure context (pipeline, task, error, links, …)."""

    channel = 'base'

    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}

    def notify(self, failure: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    # shared helpers
    @staticmethod
    def _summary(failure: Dict[str, Any]) -> str:
        task = failure.get('task_name', 'unknown')
        pipeline = failure.get('pipeline_name', 'unknown')
        return f"Task {task} FAILED in {pipeline}"

    @staticmethod
    def _error_short(failure: Dict[str, Any], limit: int = 500) -> str:
        err = failure.get('error', '')
        return str(err)[:limit] if err else 'No error details'


class WebhookNotifier(Notifier):
    """Generic JSON webhook — POSTs the failure context to an arbitrary URL.

    config: {"url_param": "/polyris/alerts/<pipeline>/webhook-url"}
    The URL is treated as a secret (it may embed a token), so it lives in SSM.
    Demonstrates how a new fire-and-forget channel slots in: one subclass.
    """

    channel = 'webhook'

    def notify(self, failure: Dict[str, Any]) -> Dict[str, Any]:
        url = _get_secret(self.config.get('url_param', ''))
        if not url:
            log.warn('webhook', 'No URL configured; skipping',
                     pipeline=failure.get('pipeline_name'))
            return {'channel': 'webhook', 'delivered': False, 'reason': 'no_url'}

        payload = {
            "event": "pipeline_task_failed",
            "summary": self._summary(failure),
            "pipeline": failure.get('pipeline_name'),
            "task": failure.get('task_name'),
            "error": self._error_short(failure),
            "date": failure.get('date', ''),
            "links": failure.get('links') or [],
        }
        try:
            status = _http_post_json(url, payload)
            ok = 200 <= status < 300
            log.info('webhook', 'Delivered' if ok else 'Non-2xx',
                     pipeline=failure.get('pipeline_name'), status=status)
            return {'channel': 'webhook', 'delivered': ok, 'status': status}
        except Exception as e:
            log.error('webhook', 'Delivery failed',
                      pipeline=failure.get('pipeline_name'),
                      error_type=type(e).__name__, error=str(e))
            return {'channel': 'webhook', 'delivered': False, 'reason': 'http_error'}


# The registry. Free channels register here directly; paid channels are added by
# the ee package below (ADR #97 explicit registration).
NOTIFIERS: Dict[str, type] = {
    'webhook': WebhookNotifier,
}


def register(channel: str, notifier_cls: type) -> None:
    """Register a notifier class for a channel. Used by the ee package to add
    paid channels (Slack, PagerDuty) without editing this file."""
    NOTIFIERS[channel] = notifier_cls


# Proprietary channels (Slack, PagerDuty) live in notify/ee/ and ship only in the
# paid build (РОЗЧЕПЛЕННЯ, ADR #98/#102). The OSS build has no ee/, so this import
# fails and the surface is webhook-only. Mirrors console_api/main.py's `import ee`.
try:
    import ee  # noqa: F401  (registers paid notifiers as an import side effect)
except ImportError:
    pass


def get_notifier(channel: str, config: Dict[str, Any]) -> Optional[Notifier]:
    """Return an instantiated notifier for a channel, or None if unknown.

    Unknown channel is a no-op (logged), not a crash — config could reference a
    channel this Lambda build doesn't ship (e.g. Slack in the OSS build).
    """
    cls = NOTIFIERS.get(channel)
    if cls is None:
        log.warn('get_notifier', 'Unknown channel; skipping', channel=channel)
        return None
    return cls(config)
