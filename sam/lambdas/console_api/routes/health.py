"""Health Check routes for Console API.

Provides system health status for monitoring and alerting.
"""
from datetime import datetime, timezone, timedelta
from typing import Dict

from botocore.exceptions import ClientError, BotoCoreError
from boto3.dynamodb.conditions import Key, Attr

from config import sfn
from constants import Limits
from dal import executions_repo, pipelines_repo, circuit_breakers_repo
from response import cors_response
from logger import log
from utils import should_skip_token_row


# Health status constants
HEALTHY = 'healthy'
DEGRADED = 'degraded'
UNHEALTHY = 'unhealthy'


def health_check(event: Dict) -> Dict:
    """
    Comprehensive health check endpoint.
    
    Checks:
    - DynamoDB connectivity and latency
    - Step Functions API availability
    - Recent pipeline execution status
    - Circuit breaker states
    
    Returns:
        200 if healthy, 503 if unhealthy
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
    
    # Calculate total check time
    elapsed_ms = (datetime.now(timezone.utc) - start_time).total_seconds() * 1000
    
    response_body = {
        'status': overall_status,
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': checks,
        'response_time_ms': round(elapsed_ms, 2)
    }
    
    status_code = 200 if overall_status in (HEALTHY, DEGRADED) else 503
    
    return cors_response(status_code, response_body)


def health_check_simple(event: Dict) -> Dict:
    """
    Simple health check for load balancer probes.
    
    Just checks if Lambda can respond - minimal latency.
    
    Returns:
        200 with {"status": "ok"}
    """
    return cors_response(200, {
        'status': 'ok',
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


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
        
        response = executions_repo.query_by_date_raw(
            KeyConditionExpression=Key('date').eq(today),
            FilterExpression=Attr('status').eq('failed') & Attr('finished_at').gte(one_hour_ago),
            Select='COUNT'
        )
        
        failure_count = response.get('Count', 0)
        
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
    metrics = {}
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
        status_counts = {}
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
        log.error("get_metrics", "Failed to compute task metrics", error=str(e))
        metrics['tasks'] = {'error': str(e)}

    # Pipeline registry count
    try:
        metrics['pipelines'] = {
            'registered': pipelines_repo.count()
        }
    except Exception as e:
        log.error("get_metrics", "Failed to count registered pipelines", error=str(e))
        metrics['pipelines'] = {'error': str(e)}
    
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
    router.add('GET', '/api/health/simple', health_check_simple)
    router.add('GET', '/api/metrics', get_metrics)
