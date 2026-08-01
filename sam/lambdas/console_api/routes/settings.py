"""Global settings routes for Console API (ADR #103 1b).

A small set of deployment-wide settings, kept in one reserved registry record so
the SFN can read them with the same getItem it uses for alert_config.

GET  /api/settings/decision-timeout   → the current global decision-wait timeout

The GET is free — everyone can *see* the value (the UI shows it read-only on the
free tier). The PUT that edits it is Team-tier and lives in console_api/ee/, so
the free build exposes the value but not the ability to change it.
"""
from typing import Dict

from dal import pipelines_repo
from response import cors_response
from logger import log


def get_decision_timeout(event: Dict) -> Dict:
    """Return the global decision-wait timeout (seconds). Read-only here; editing
    is a Team-tier action handled by the ee route."""
    try:
        settings = pipelines_repo.get_global_settings()
        return cors_response(200, {
            'decision_timeout_seconds': settings['decision_timeout_seconds'],
        })
    except Exception as e:
        log.error('settings', 'Failed to read decision timeout',
                  error_type=type(e).__name__, error=str(e))
        # Degrade to the documented default rather than erroring the UI.
        return cors_response(200, {
            'decision_timeout_seconds': pipelines_repo.DEFAULT_DECISION_TIMEOUT_SECONDS,
        })


def register(router) -> None:
    """Register the free settings routes (read-only)."""
    router.add('GET', '/api/settings/decision-timeout', get_decision_timeout, None)
