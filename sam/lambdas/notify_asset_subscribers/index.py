"""
Notify Asset Subscribers Lambda

Notifies tasks that are waiting for asset dependencies (wait_for).
Called after a task with outlets completes successfully.

Input:
{
    "outlets": [
        {"name": "inventory", "uri": "s3://..."}
    ],
    "source_task": "producer_task",
    "source_dag": "producer_pipeline",
    "event_time": "2026-01-26T08:00:00Z"
}

Output:
{
    "notified": 3,
    "assets": ["inventory"],
    "subscribers": ["task_a", "task_b", "task_c"]
}
"""

import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Any

# v0.79.3 (ADR #75) — DAL repository pattern for DynamoDB.
# Raw boto3 calls moved to dal/__init__.py.
from dal import subscriptions_repo, asset_events_repo, tokens_repo
# v0.79.4 (ADR #76) — structured JSON logging.
from logger import log

# Lazy initialization for SFN + Lambda clients (single-op boto3 calls, no
# scope advantage from a repo).
_sfn = None
_lambda_client = None


def _get_sfn():
    global _sfn
    if _sfn is None:
        import boto3
        _sfn = boto3.client('stepfunctions')
    return _sfn


def _get_lambda():
    global _lambda_client
    if _lambda_client is None:
        import boto3
        _lambda_client = boto3.client('lambda')
    return _lambda_client


SUBSCRIPTIONS_TABLE = os.environ.get('SUBSCRIPTIONS_TABLE', 'dependency-subscriptions')
ASSET_EVENTS_TABLE = os.environ.get('ASSET_EVENTS_TABLE', 'asset-events')
EVALUATE_DEPS_LAMBDA = os.environ.get('EVALUATE_DEPS_LAMBDA', '')


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Main handler - notify asset subscribers."""
    
    outlets = event.get('outlets', [])
    event_time = event.get('event_time', '')
    
    if not outlets:
        return {
            'notified': 0,
            'assets': [],
            'subscribers': []
        }
    
    notified_count = 0
    notified_subscribers = []
    asset_names = []
    
    for outlet in outlets:
        asset_name = outlet.get('name', '')
        asset_uri = outlet.get('uri', '')
        
        if not asset_name:
            continue
        
        asset_names.append(asset_name)
        
        # Find and notify subscribers for this asset
        subscribers = _get_asset_subscribers(asset_name)
        
        for sub in subscribers:
            wait_token = sub.get('wait_token', '')
            subscriber_name = sub.get('subscriber', '')
            freshness_hours = sub.get('freshness_hours')
            subscription_type = sub.get('subscription_type', 'asset')
            
            if not wait_token:
                continue
            
            # Consecutive check: re-verify all dates before signaling
            if subscription_type == 'asset_consecutive':
                consecutive_days = int(sub.get('consecutive_days', 1))
                reference_date = sub.get('reference_date', '')
                if not _check_consecutive_ready(asset_name, consecutive_days, reference_date):
                    # Not all dates present yet - keep subscription, don't notify
                    continue
            
            # Check freshness if subscriber requires it
            elif freshness_hours:
                # Re-check freshness at notify time
                if not _check_freshness(asset_name, event_time, freshness_hours):
                    # Asset is stale for this subscriber - don't notify yet
                    continue
            
            # Case B coordination: flip assets_ready=true on the subscriber's
            # task record and delegate the ready/wait decision to evaluate_deps.
            # Reason: this task may also have task_deps that haven't finished
            # yet. Signaling now would wake dep_wrapper and run the task with
            # pending upstreams (xcom.pull → PullError). evaluate_deps is the
            # single gate that checks BOTH task_deps AND assets_ready.
            execution_name = sub.get('execution_name', '')
            should_signal = _coordinate_ready_check(execution_name, asset_name)

            # Always delete the subscription — the asset has arrived, so this
            # subscription's job is done. If task_deps aren't ready yet, they'll
            # signal via notify_dependents later (and evaluate_deps will then
            # see assets_ready=true on the record).
            _delete_subscription(asset_name, subscriber_name)

            if not should_signal:
                # Task_deps not ready — leave the wait_token untouched. When
                # the last task_dep completes, notify_dependents' evaluate_deps
                # will see assets_ready=true and signal.
                continue

            # Send signal to waiting task
            success = _send_task_success(
                wait_token,
                asset_name=asset_name,
                asset_uri=asset_uri,
                event_time=event_time
            )

            if success:
                notified_count += 1
                notified_subscribers.append(subscriber_name)
    
    return {
        'notified': notified_count,
        'assets': asset_names,
        'subscribers': notified_subscribers
    }


def _get_asset_subscribers(asset_name: str) -> List[Dict]:
    """Get all subscribers waiting for this asset (with pagination)."""
    try:
        # v0.79.3 (ADR #75) — DAL handles query + pagination + 10000 cap.
        subs = subscriptions_repo.list_for_asset(asset_name)
        if len(subs) >= 10000:
            log.warn("_get_asset_subscribers", "Hit 10000 subscriber pagination cap", asset_name=asset_name)
        return subs
    except Exception as e:
        # Subscriber query is the entire wait_for wake-up path. If it fails,
        # every pull-waiter for this asset hangs to its own orchestration
        # timeout — no retry at this layer. ERROR level (not WARN) so DDB
        # schema or throttling issues are visible in CloudWatch instead of
        # hiding behind "task just didn't resume."
        log.error("_get_asset_subscribers", "Query failed — pull-waiters will not be woken for this event",
                  asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return []


def _check_freshness(asset_name: str, event_time: str, freshness_hours: float) -> bool:
    """Check if asset event meets freshness requirement."""
    try:
        if 'Z' in event_time:
            event_time = event_time.replace('Z', '+00:00')
        event_dt = datetime.fromisoformat(event_time)
        
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        age_hours = (now - event_dt).total_seconds() / 3600
        
        return age_hours <= freshness_hours
    except Exception as e:
        # Preserve "default to fresh" behavior so downstream orchestration isn't
        # blocked by a single bad timestamp. ERROR level (not WARN) because
        # "default to fresh" is fail-OPEN — a systematic timestamp issue
        # silently triggers every consumer on stale data until someone notices
        # the warning sea. Visible as error is the mitigation until the
        # behavior is revisited (see experimental limitations in ASSETS.md).
        log.error("_check_freshness", "Parse failed; defaulting to fresh (fail-open)",
                  asset_name=asset_name,
                  event_time=event_time,
                  error_type=type(e).__name__,
                  error=str(e))
        return True  # Default to fresh if parsing fails


def _check_consecutive_ready(asset_name: str, consecutive_days: int, reference_date: str) -> bool:
    """
    Check if asset has events for N consecutive dates ending at reference_date.
    
    Used by notify to re-verify before sending signal to consecutive subscribers.
    Returns True only when ALL required dates have events.
    """
    from datetime import timedelta
    
    # Defensive: consecutive_days must be >= 1
    if consecutive_days < 1:
        return False
    
    if not reference_date:
        reference_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    try:
        ref_dt = datetime.strptime(reference_date, '%Y-%m-%d')
    except ValueError:
        return False
    
    # Build required dates set
    required_dates = set()
    for i in range(consecutive_days):
        dt = ref_dt - timedelta(days=i)
        required_dates.add(dt.strftime('%Y-%m-%d'))
    
    # Query recent events
    # v0.79.3 (ADR #75) — DAL handles the boto3 dance.
    try:
        items = asset_events_repo.query_recent(
            asset_name, limit=max(consecutive_days * 5, 30)
        )
    except Exception as e:
        # Wake-up-path catch: a `consecutive()` requirement that can't query
        # asset events fails-closed below (returns False → skips signal → task
        # keeps waiting). ERROR level so a systematic query issue is visible
        # in CloudWatch instead of being lost in warn-noise.
        log.error("_check_consecutive", "Failed to query asset events",
                  asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return False

    # Extract distinct execution_dates
    found_dates = set()
    for item in items:
        exec_date = item.get('execution_date', '')
        if exec_date:
            found_dates.add(exec_date)
    
    # Check if all required dates are present
    missing = required_dates - found_dates
    return len(missing) == 0


def _coordinate_ready_check(execution_name: str, asset_name: str) -> bool:
    """AND-gate + Case B coordination. Returns True iff we should signal wait_token.

    1) Atomically ADD asset_name to `assets_ready_set` on the task record.
    2) Check AND completeness: if `wait_for_assets` is set on the record and
       not all required assets have arrived yet, return False — this asset
       arrived but others are still pending. The remaining subscriptions will
       each call this function when their asset arrives.
    3) If all assets satisfied (or no `wait_for_assets` set — legacy path),
       ask evaluate_deps whether task_deps are also done.
    4) On any DDB/Lambda failure, fail-open: signal anyway rather than hang."""
    if not execution_name:
        return True  # legacy path — old subscriptions may lack execution_name

    record = tokens_repo.add_to_assets_ready_set_and_get(execution_name, asset_name)
    if not record:
        log.warn("_coordinate_ready_check", "task record not found",
                 execution_name=execution_name)
        return True  # fall back to legacy behaviour

    # AND-completeness check: all subscribed assets must arrive before we signal.
    wait_for_assets = record.get('wait_for_assets')  # SS or absent (legacy)
    if wait_for_assets:
        assets_ready_set = record.get('assets_ready_set') or set()
        if not set(wait_for_assets) <= set(assets_ready_set):
            # More assets still pending — don't signal yet. The last arriving
            # asset's call will find the sets equal and proceed.
            return False

    if not EVALUATE_DEPS_LAMBDA:
        # Coordinated evaluation not wired — a fresh deploy may still be
        # rolling out. Signal anyway.
        return True

    try:
        dependencies_raw = record.get('dependencies', '[]')
        wait_for_raw = record.get('wait_for', '[]')
        payload = {
            'dependencies': json.loads(dependencies_raw) if isinstance(dependencies_raw, str) else dependencies_raw,
            'trigger_rule': record.get('trigger_rule', 'all_success'),
            'date': record.get('date', ''),
            'pipeline_execution_short': record.get('pipeline_execution_short', ''),
            'pipeline_execution': record.get('pipeline_execution', ''),
            'wait_for': json.loads(wait_for_raw) if isinstance(wait_for_raw, str) else wait_for_raw,
            # Pass set-based fields for evaluate_deps' AND check; also keep
            # legacy bool for older evaluate_deps deployments still in rollout.
            # wait_for_assets must be None (not []) when absent so evaluate_deps
            # can distinguish "no gate" (OR-group / legacy row) from "empty gate".
            'wait_for_assets': (list(record['wait_for_assets']) if 'wait_for_assets' in record else None),
            'assets_ready_set': list(record.get('assets_ready_set') or []),
            'assets_ready': True,
        }
        response = _get_lambda().invoke(
            FunctionName=EVALUATE_DEPS_LAMBDA,
            InvocationType='RequestResponse',
            Payload=json.dumps(payload).encode('utf-8'),
        )
        body = json.loads(response['Payload'].read())
        return bool(body.get('is_ready', False))
    except Exception as e:
        log.error("_coordinate_ready_check", "evaluate_deps invoke failed; signaling anyway (fail-open)",
                  execution_name=execution_name, error_type=type(e).__name__, error=str(e))
        return True


def _send_task_success(wait_token: str, asset_name: str, asset_uri: str, event_time: str) -> bool:
    """Send task success signal to waiting task."""
    sfn = _get_sfn()
    
    output = json.dumps({
        'signal': 'asset_ready',
        'asset_name': asset_name,
        'asset_uri': asset_uri,
        'event_time': event_time
    })
    
    try:
        sfn.send_task_success(
            taskToken=wait_token,
            output=output
        )
        return True
    except sfn.exceptions.TaskTimedOut:
        log.warn("_send_task_success", "Task token timed out", asset_name=asset_name)
        return False
    except sfn.exceptions.InvalidToken:
        log.warn("_send_task_success", "Invalid task token", asset_name=asset_name)
        return False
    except sfn.exceptions.TaskDoesNotExist:
        log.warn("_send_task_success", "Task does not exist", asset_name=asset_name)
        return False
    except Exception as e:
        # The three SFN-specific exceptions above are legitimate "token is no
        # longer valid" cases (timed out, task already consumed, task cleaned
        # up) — WARN is right for those. This branch catches everything else:
        # IAM denial, service outage, malformed output. Those fail the wake-
        # up without a retry at this layer, so pull-waiters stay blocked.
        # ERROR so a systematic send_task_success issue is visible.
        log.error("_send_task_success", "send_task_success failed — pull-waiter not woken",
                  asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return False


def _delete_subscription(asset_name: str, subscriber: str):
    """Delete subscription after successful notify."""
    try:
        # v0.79.3 (ADR #75) — DAL owns the composite-key shape.
        subscriptions_repo.delete(asset_name, subscriber)
    except Exception as e:
        log.warn("_delete_subscription", "Delete failed", asset_name=asset_name, subscriber=subscriber, error=str(e))
