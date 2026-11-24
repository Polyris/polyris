"""Health Check routes for Console API.

Provides system health status for monitoring and alerting. The routes are
public (auth.PUBLIC_PATH_PREFIXES), so probes and monitors need no
credentials. GET /api/health shows each caller what it may see:
    * a caller with a valid token, or any caller while auth is off, gets every
      check's details and the findings the console's System status shows
      (detail "full");
    * anyone else gets each check's status word only (detail "minimal"): the
      details hold AWS error text, resource names and the account ID.
The status code is the same in both: 200 healthy or degraded, 503 unhealthy.
"""
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from botocore.exceptions import ClientError, BotoCoreError
from boto3.dynamodb.conditions import Key, Attr

from auth import is_auth_enabled, optional_principal
from config import sfn
from constants import Limits
from polyris.coordination import sweep
from dal import api_tokens_repo, executions_repo, pipelines_repo, circuit_breakers_repo
from response import cors_response
from logger import log
from utils import should_skip_token_row


# Health status constants
HEALTHY = 'healthy'
DEGRADED = 'degraded'
UNHEALTHY = 'unhealthy'

# How much of /api/health the caller sees (module docstring).
FULL = 'full'
MINIMAL = 'minimal'

#: The console's own findings for an AWS service it cannot reach: (check, title, impact).
_UNREACHABLE = (
    ('dynamodb', 'The console cannot reach DynamoDB', 'The console cannot read or change pipelines and tasks.'),
    ('stepfunctions', 'The console cannot reach Step Functions', 'The console cannot start or stop runs.'),
)
_UNREACHABLE_FIX = "Check the console API's role and the AWS Health Dashboard, then read the console API's logs."

#: What the public /api/metrics says for a figure it could not compute; the error itself goes to the log.
METRICS_UNAVAILABLE = 'Not available: the console API logs have the error.'


def health_check(event: Dict) -> Dict:
    """
    Health check endpoint.

    Checks:
    - DynamoDB connectivity and latency
    - Step Functions API availability
    - Recent pipeline execution status
    - Circuit breaker states
    - The coordination engine (the sweeper's last report)

    Returns:
        200 if healthy or degraded, 503 if unhealthy; with every detail and the
        findings, or each check's status word only (module docstring)
    """
    checks = {}
    overall_status = HEALTHY
    start_time = datetime.now(timezone.utc)
    
    # 1. DynamoDB Health Check
    checks['dynamodb'] = _check_dynamodb()
    if checks['dynamodb']['status'] == UNHEALTHY:
        overall_status = UNHEALTHY
    elif checks['dynamodb']['status'] == DEGRADED and overall_status == HEALTHY:
        overall_status = DEGRADED
    
    # 2. Step Functions Health Check
    checks['stepfunctions'] = _check_stepfunctions()
    if checks['stepfunctions']['status'] == UNHEALTHY:
        overall_status = UNHEALTHY
    elif checks['stepfunctions']['status'] == DEGRADED and overall_status == HEALTHY:
        overall_status = DEGRADED
    
    # 3. Recent Failures Check
    checks['recent_failures'] = _check_recent_failures()
    if checks['recent_failures']['status'] == UNHEALTHY:
        overall_status = DEGRADED  # Failures don't make system unhealthy, just degraded
    
    # 4. Circuit Breaker States
    checks['circuit_breakers'] = _check_circuit_breakers()
    if checks['circuit_breakers']['status'] == UNHEALTHY:
        overall_status = DEGRADED

    # 5. The coordination engine (ADR #130 §9). The console's System status shows its findings
    # while it is unhealthy; the API itself still serves, so the answer stays degraded, not 503.
    checks['coordination'], unreadable, engine = _check_coordination()
    if checks['coordination']['status'] != HEALTHY and overall_status == HEALTHY:
        overall_status = DEGRADED

    # Calculate total check time
    elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000

    response_body = {
        'status': overall_status,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': checks,
        'response_time_ms': round(elapsed_ms, 2)
    }

    status_code = 200 if overall_status in (HEALTHY, DEGRADED) else 503

    if not _shows_details(event):
        return cors_response(status_code, _minimal(response_body))
    response_body['detail'] = FULL
    # The console's own findings first, then the engine's.
    response_body['findings'] = ([unreadable] if unreadable else []) + _unreachable(checks) + engine
    return cors_response(status_code, response_body)


def health_check_live(event: Dict) -> Dict:
    """
    Liveness probe for load balancers and monitors.

    Answers as soon as the Lambda runs: no downstream call, nothing to hide.

    Returns:
        200 with {"status": "ok"}
    """
    return cors_response(200, {
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


def _shows_details(event: Dict) -> bool:
    """Whether the caller may see the details: auth is off, or it sent a valid token.

    A token that is missing, invalid or cannot be verified shows the minimal body
    (auth.optional_principal fails closed); it is never a 401 on this public route.
    """
    return not is_auth_enabled() or optional_principal(event, api_tokens_repo) is not None


def _minimal(body: Dict) -> Dict:
    """The body for a caller without a valid token, built from an allowlist: each check's
    status word, and nothing a check says (messages, the report, names, AWS error text)."""
    return {
        'status': body['status'],
        'timestamp': body['timestamp'],
        'response_time_ms': body['response_time_ms'],
        'detail': MINIMAL,
        'checks': {name: {'status': check['status']} for name, check in body['checks'].items()},
    }


def _unreachable(checks: Dict) -> List[Dict[str, str]]:
    """A finding for each AWS service the console cannot reach, naming the check's message."""
    findings = []
    for name, title, impact in _UNREACHABLE:
        if checks[name]['status'] == UNHEALTHY:
            finding = {'title': title, 'resource': str(checks[name].get('message', '')), 'impact': impact,
                       'fix': _UNREACHABLE_FIX}
            findings.append({k: v for k, v in finding.items() if v})
    return findings


def _unreadable(e: Exception) -> Dict[str, str]:
    """The console's finding when the engine health row cannot be read or judged."""
    return {
        'title': 'The coordination status cannot be read',
        'resource': f'{type(e).__name__}: {e}',
        'impact': 'The console cannot tell whether waiting tasks are re-checked.',
        'fix': "Check the console API's access to the pipeline-tokens table, then read the console API's logs.",
    }


def _check_coordination() -> Tuple[Dict, Optional[Dict[str, str]], List[Dict[str, str]]]:
    """The sweeper's last report, judged by the same rule the sweeper uses (sweep.engine_findings).

    Returns the check, the console's own finding when the row cannot be read or
    judged, and the engine's findings. Staleness is judged here, from the
    report's own time: a sweeper that stopped running cannot report that it stopped.
    """
    try:
        report = executions_repo.get(sweep.HEALTH_KEY)
    except (ClientError, BotoCoreError) as e:
        log.error("health", "Could not read the engine health row", error=str(e))
        return {'status': UNHEALTHY, 'report': {}}, _unreadable(e), []
    try:
        findings = sweep.engine_findings(report, datetime.now(timezone.utc))
        shown = {k: report[k] for k in ('checked_at', 'fanout_mapping', 'fanout_result', 'stream', 'oldest_overdue',
                                        'failed_steps') if report and k in report}
        if report and 'push_retry_backlog' in report:
            shown['push_retry_backlog'] = int(report['push_retry_backlog'])     # a DynamoDB Decimal
    except (AttributeError, TypeError, ValueError) as e:
        # A row no writer produces, edited by hand or written by a bug: name it, as for a row that cannot be
        # read. A 500 here would fail all of /api/health, so the console could not show what is wrong (Principle #51).
        log.error("health", "The engine health row cannot be judged", error=str(e))
        return {'status': UNHEALTHY, 'report': {}}, _unreadable(e), []
    return {'status': UNHEALTHY if findings else HEALTHY, 'report': shown}, None, findings


def _check_dynamodb() -> Dict:
    """Check DynamoDB connectivity and latency."""
    try:
        start = datetime.now(timezone.utc)
        
        # Simple read to check connectivity
        executions_repo.health_ping()
        
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        
        # Latency thresholds
        if latency_ms > 1000:
            return {
                'status': DEGRADED,
                'latency_ms': round(latency_ms, 2),
                'message': 'High latency'
            }
        
        return {
            'status': HEALTHY,
            'latency_ms': round(latency_ms, 2)
        }
        
    except ClientError as e:
        return {
            'status': UNHEALTHY,
            'error': e.response['Error']['Code'],
            'message': str(e)
        }
    except Exception as e:
        log.error("_check_dynamodb", "Unexpected error", error=str(e), error_type=type(e).__name__)
        return {
            'status': UNHEALTHY,
            'error': type(e).__name__,
            'message': str(e)
        }


def _check_stepfunctions() -> Dict:
    """Check Step Functions API availability."""
    try:
        start = datetime.now(timezone.utc)
        
        # List state machines (lightweight call)
        sfn.list_state_machines(maxResults=1)
        
        latency_ms = (datetime.now(timezone.utc) - start).total_seconds() * 1000
        
        if latency_ms > 2000:
            return {
                'status': DEGRADED,
                'latency_ms': round(latency_ms, 2),
                'message': 'High latency'
            }
        
        return {
            'status': HEALTHY,
            'latency_ms': round(latency_ms, 2)
        }
        
    except Exception as e:
        log.error("_check_stepfunctions", "Unexpected error", error=str(e), error_type=type(e).__name__)
        return {
            'status': UNHEALTHY,
            'error': type(e).__name__,
            'message': str(e)
        }


def _check_recent_failures() -> Dict:
    """Check for recent pipeline failures."""
    try:
        # Query recent failures (last hour)
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        
        # The items, not a count: a count would include backfill records, which carry a date (ADR #137 §5).
        response = executions_repo.query_by_date_raw(
            KeyConditionExpression=Key('date').eq(today),
            FilterExpression=Attr('status').eq('failed') & Attr('finished_at').gte(one_hour_ago),
            ProjectionExpression='execution_name, pipeline_name',
        )

        failure_count = len(response.get('Items', []))
        
        if failure_count > 10:
            return {
                'status': UNHEALTHY,
                'failures_last_hour': failure_count,
                'message': 'High failure rate'
            }
        elif failure_count > 0:
            return {
                'status': DEGRADED,
                'failures_last_hour': failure_count
            }
        
        return {
            'status': HEALTHY,
            'failures_last_hour': 0
        }
        
    except Exception as e:
        log.error("_check_recent_failures", "Unexpected error", error=str(e), error_type=type(e).__name__)
        return {
            'status': HEALTHY,  # Don't fail health check if we can't count failures
            'error': str(e)
        }


def _check_circuit_breakers() -> Dict:
    """Check circuit breaker states.

    The circuit-breaker table is optional (deployment-specific). When the
    feature is not configured, the repo's `enabled` flag is False and we
    short-circuit to HEALTHY.
    """
    if not circuit_breakers_repo.enabled:
        return {'status': HEALTHY, 'message': 'Circuit breakers not configured'}

    try:
        open_circuits = circuit_breakers_repo.query_open()

        if open_circuits:
            return {
                'status': UNHEALTHY,
                'open_circuits': [item['service_name'] for item in open_circuits],
                'message': f"{len(open_circuits)} circuit(s) open"
            }

        return {
            'status': HEALTHY,
            'open_circuits': []
        }

    except (ClientError, BotoCoreError) as e:
        log.error("_check_circuit_breakers", "AWS error querying circuit-breaker table",
                  error=str(e), error_type=type(e).__name__)
        return {
            'status': HEALTHY,  # Don't fail health check if breakers can't be read
            'error': str(e)
        }
    except Exception as e:
        log.error("_check_circuit_breakers", "Unexpected error", error=str(e), error_type=type(e).__name__)
        return {
            'status': HEALTHY,
            'error': str(e)
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Metrics endpoint for custom CloudWatch metrics
# ═══════════════════════════════════════════════════════════════════════════════

def get_metrics(event: Dict) -> Dict:
    """
    Get system metrics for dashboards.
    
    Returns:
        Current system metrics and statistics
    """
    metrics: Dict[str, Dict[str, Any]] = {}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    try:
        # Query today's tasks
        # Per CLAUDE.md: MUST filter _ prefixed internal records (e.g. _notify_warn_*)
        # to avoid skewing failed/success counts.
        items = executions_repo.query_by_date(
            today,
            max_items=Limits.MAX_FETCH_ITEMS,
            projection='execution_name, #s',
            expr_names={'#s': 'status'}
        )

        # Filter out internal records once, then count
        real_items = [i for i in items if not should_skip_token_row(i)]
        status_counts: Dict[str, int] = {}
        for item in real_items:
            status = item.get('status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1

        total = len(real_items)
        success = status_counts.get('success', 0)
        failed = status_counts.get('failed', 0)
        running = status_counts.get('running', 0) + status_counts.get('pending', 0)

        metrics['tasks'] = {
            'total': total,
            'success': success,
            'failed': failed,
            'running': running,
            'success_rate': round(success / total * 100, 1) if total > 0 else 0,
            'by_status': status_counts
        }

    except Exception as e:
        # A public route: the error text names AWS resources and the account, so it stays in the log.
        log.error("get_metrics", "Failed to compute task metrics", error=str(e))
        metrics['tasks'] = {'error': METRICS_UNAVAILABLE}

    # Pipeline registry count
    try:
        metrics['pipelines'] = {
            'registered': pipelines_repo.count()
        }
    except Exception as e:
        log.error("get_metrics", "Failed to count registered pipelines", error=str(e))
        metrics['pipelines'] = {'error': METRICS_UNAVAILABLE}
    
    return cors_response(200, {
        'date': today,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'metrics': metrics
    })


def register(router) -> None:
    """Register Health/metrics routes (open-core). See ADR #97.

    Reference migration of a route module to explicit self-registration. These
    paths are public (auth gate excludes them via ``is_public_path``).
    """
    router.add('GET', '/api/health', health_check)
    router.add('GET', '/api/health/live', health_check_live)
    router.add('GET', '/api/metrics', get_metrics)
