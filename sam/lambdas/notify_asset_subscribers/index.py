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
from typing import List, Dict, Any, Optional, Tuple

from botocore.exceptions import ClientError

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
            should_signal, record = _coordinate_ready_check(execution_name, asset_name)

            if not should_signal:
                # Task_deps not ready, or AND gate not yet satisfied. Asset
                # has arrived and is tracked in assets_ready_set — delete
                # the subscription now. When the last remaining dep or asset
                # arrives, notify_dependents / the last-asset path will signal.
                _delete_subscription(asset_name, subscriber_name)
                continue

            # Send signal first. Only delete the subscription after the signal
            # reaches SFN — if _send_task_success returns 'failed', the
            # subscription is kept so the next asset event can retry.
            signal_result = _send_task_success(
                wait_token,
                asset_name=asset_name,
                asset_uri=asset_uri,
                event_time=event_time,
            )

            if signal_result == 'failed':
                # Transient / IAM failure — keep subscription for retry.
                # Write a _notify_warn_ record so the operator sees it in the UI.
                _write_signal_failure_warn(execution_name, subscriber_name, record, asset_name)
                continue

            # Signal reached SFN (or token already consumed) — subscription done.
            _delete_subscription(asset_name, subscriber_name)

            # B3: for OR/mixed specs (no gate), clean up sibling subscriptions
            # so later events on those assets don't re-evaluate an already-done task.
            # Pure-AND rows always have wait_for_assets; skip them (each asset's
            # subscription is deleted when it arrives, not here).
            if record and 'wait_for_assets' not in record and execution_name:
                wait_for_json = record.get('wait_for', '[]')
                _cleanup_or_siblings(wait_for_json, asset_name, subscriber_name, execution_name)

            if signal_result == 'sent':
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


def _coordinate_ready_check(
    execution_name: str, asset_name: str
) -> Tuple[bool, Optional[Dict]]:
    """AND-gate + Case B coordination. Returns (should_signal, record).

    1) Atomically ADD asset_name to `assets_ready_set` on the task record.
    2) Check AND completeness: if `wait_for_assets` is set on the record and
       not all required assets have arrived yet, return (False, record) — this
       asset arrived but others are still pending. The remaining subscriptions
       will each call this function when their asset arrives.
    3) If all assets satisfied (or no `wait_for_assets` set — legacy path),
       ask evaluate_deps whether task_deps are also done.
    4) On any DDB/Lambda failure, fail-open: return (True, None)."""
    if not execution_name:
        return True, None  # legacy path — old subscriptions may lack execution_name

    record = tokens_repo.add_to_assets_ready_set_and_get(execution_name, asset_name)
    if not record:
        log.warn("_coordinate_ready_check", "task record not found",
                 execution_name=execution_name)
        return True, None  # fall back to legacy behaviour

    # AND-completeness check: all subscribed assets must arrive before we signal.
    wait_for_assets = record.get('wait_for_assets')  # SS or absent (legacy)
    if wait_for_assets:
        assets_ready_set = record.get('assets_ready_set') or set()
        if not set(wait_for_assets) <= set(assets_ready_set):
            # More assets still pending — don't signal yet. The last arriving
            # asset's call will find the sets equal and proceed.
            return False, record

    if not EVALUATE_DEPS_LAMBDA:
        # Coordinated evaluation not wired — a fresh deploy may still be
        # rolling out. Signal anyway.
        return True, record

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
        return bool(body.get('is_ready', False)), record
    except Exception as e:
        log.error("_coordinate_ready_check", "evaluate_deps invoke failed; signaling anyway (fail-open)",
                  execution_name=execution_name, error_type=type(e).__name__, error=str(e))
        return True, record


def _send_task_success(
    wait_token: str, asset_name: str, asset_uri: str, event_time: str
) -> str:
    """Send task success signal. Returns 'sent', 'token_consumed', or 'failed'.

    'sent'           — SFN accepted the signal.
    'token_consumed' — token already timed-out, consumed, or task gone; subscription
                       should be deleted but the caller must NOT write _notify_warn_.
    'failed'         — transient / IAM failure; caller should keep the subscription
                       for retry and write a _notify_warn_ record.
    """
    sfn = _get_sfn()

    output = json.dumps({
        'signal': 'asset_ready',
        'asset_name': asset_name,
        'asset_uri': asset_uri,
        'event_time': event_time,
    })

    try:
        sfn.send_task_success(taskToken=wait_token, output=output)
        return 'sent'
    except sfn.exceptions.TaskTimedOut:
        log.warn("_send_task_success", "Task token timed out", asset_name=asset_name)
        return 'token_consumed'
    except sfn.exceptions.InvalidToken:
        log.warn("_send_task_success", "Invalid task token", asset_name=asset_name)
        return 'token_consumed'
    except sfn.exceptions.TaskDoesNotExist:
        log.warn("_send_task_success", "Task does not exist", asset_name=asset_name)
        return 'token_consumed'
    except Exception as e:
        # IAM denial, service outage, malformed output — not a "token consumed"
        # case. Caller keeps the subscription and writes _notify_warn_.
        log.error("_send_task_success", "send_task_success failed — pull-waiter not woken",
                  asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return 'failed'


def _write_signal_failure_warn(
    execution_name: str,
    subscriber_name: str,
    record: Optional[Dict],
    asset_name: str,
) -> None:
    """Write a _notify_warn_ record when send_task_success fails (subscription kept).
    The UI bell will show the failure so an operator can investigate."""
    if not record:
        return  # no record context — ERROR already logged by _send_task_success
    try:
        tokens_repo.write_warn(
            execution_name=execution_name,
            task_name=subscriber_name,
            pipeline_execution=record.get('pipeline_execution', ''),
            pipeline_name=record.get('pipeline_name', ''),
            date=record.get('date', ''),
            error=(
                f'send_task_success failed for asset {asset_name!r}; '
                'subscription kept for visibility — operator should re-run manually'
            ),
        )
    except ClientError as e:
        if e.response['Error']['Code'] == 'AccessDeniedException':
            raise
        log.error("_write_signal_failure_warn", "Failed to write warn record",
                  execution_name=execution_name, error=str(e))
    except Exception as e:
        log.error("_write_signal_failure_warn", "Failed to write warn record",
                  execution_name=execution_name, error=str(e))


def _delete_subscription(asset_name: str, subscriber: str):
    """Delete subscription after successful notify."""
    try:
        # v0.79.3 (ADR #75) — DAL owns the composite-key shape.
        subscriptions_repo.delete(asset_name, subscriber)
    except Exception as e:
        log.warn("_delete_subscription", "Delete failed", asset_name=asset_name, subscriber=subscriber, error=str(e))


def _flatten_asset_names(wait_for) -> set:
    """Return the flat set of all asset names in a wait_for spec (any nesting depth).

    Duplicated from check_assets for Lambda isolation (the two packages share
    the name 'index'/'dal' so cannot import each other). Parity test:
    tests/backend/test_flatten_asset_names_parity.py.
    """
    if not wait_for:
        return set()
    names: set = set()
    for item in wait_for:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            if 'name' in item:
                names.add(item['name'])
            elif 'assets' in item:
                names |= _flatten_asset_names(item['assets'])
    return names


def _cleanup_or_siblings(
    wait_for_json: str,
    waking_asset: str,
    subscriber: str,
    execution_name: str,
) -> None:
    """After a successful OR-group signal, delete subscriptions for the other
    members so they don't trigger spurious evaluate_deps calls on future events.

    Only deletes rows whose execution_name matches this run (ownership guard).
    ConditionalCheckFailed means the row belongs to a different run — safe no-op.
    """
    try:
        import json as _json
        wait_for = _json.loads(wait_for_json) if isinstance(wait_for_json, str) else wait_for_json
    except Exception:
        return

    sibling_names = _flatten_asset_names(wait_for) - {waking_asset}
    for asset_name in sibling_names:
        try:
            deleted = subscriptions_repo.delete_if_owned(asset_name, subscriber, execution_name)
            if not deleted:
                log.debug("_cleanup_or_siblings", "Sibling belongs to another execution; skipped",
                          asset_name=asset_name, subscriber=subscriber)
        except Exception as e:
            log.warn("_cleanup_or_siblings", "Delete failed", asset_name=asset_name, error=str(e))
