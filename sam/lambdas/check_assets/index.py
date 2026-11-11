"""
Check Assets Lambda

Checks if asset dependencies (wait_for) are satisfied and saves subscriptions
for assets that are not yet ready.

Input:
{
    "wait_for": [
        {"asset_name": "inventory", "freshness_hours": null},
        {"asset_name": "catalog", "freshness_hours": 24}
    ],
    "task_name": "consumer_task",
    "wait_token": "AQC...",
    "execution_name": "consumer_task-2026-01-26-abc123",
    "ttl": 1737907200
}

Output:
{
    "type": "asset_deps",
    "ready": true|false,
    "assets": [
        {"name": "inventory", "ready": true, "event_time": "2026-01-26T08:00:00Z", "uri": "s3://..."},
        {"name": "catalog", "ready": false, "reason": "no_event"|"stale"}
    ]
}
"""

import os
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

# v0.79.3 (ADR #75) — DAL repository pattern for all DynamoDB access.
from dal import asset_events_repo, subscriptions_repo, tokens_repo
# v0.79.4 (ADR #76) — structured JSON logging.
from logger import log


ASSET_EVENTS_TABLE = os.environ.get('ASSET_EVENTS_TABLE', 'asset-events')
SUBSCRIPTIONS_TABLE = os.environ.get('SUBSCRIPTIONS_TABLE', 'dependency-subscriptions')


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Main handler - check asset freshness and save subscriptions."""
    
    wait_for = event.get('wait_for', [])
    task_name = event.get('task_name', '')
    wait_token = event.get('wait_token', '')
    execution_name = event.get('execution_name', '')
    ttl = event.get('ttl', 0)
    current_date = event.get('current_date', '')
    
    # Handle empty wait_for
    if not wait_for:
        return {
            'type': 'asset_deps',
            'ready': True,
            'assets': []
        }
    
    # Evaluate wait_for with AND/OR logic
    is_ready, asset_results = _evaluate_wait_for(wait_for, current_date)

    # If not ready, save subscriptions for missing assets
    if not is_ready:
        missing_assets = _get_missing_assets(asset_results)
        missing_names = [spec['name'] for spec in missing_assets]

        # Computed once: OR-group spec disables the AND gate entirely.
        is_pure_and = not _has_or_group(wait_for)

        # Gate written before subscriptions: an event arriving between a
        # subscription write and the gate write sees no wait_for_assets field
        # and falls back to first-arrival (OR) semantics. With the gate present
        # first, every arriving asset encounters a complete AND gate immediately.
        # Skipped for specs that contain an OR group — first-arrival semantics
        # apply there; the gate is not needed.
        if is_pure_and:
            _write_pending_assets(execution_name, missing_names)

        for asset_spec in missing_assets:
            _save_asset_subscription(
                asset_name=asset_spec['name'],
                subscriber=task_name,
                wait_token=wait_token,
                execution_name=execution_name,
                freshness_hours=asset_spec.get('freshness_hours'),
                consecutive_days=asset_spec.get('consecutive_days'),
                reference_date=current_date,
                ttl=ttl
            )

        # Double-check: Re-evaluate after saving subscriptions to handle race condition
        # Producer might have notified between initial check and subscription save
        is_ready_recheck, asset_results_recheck = _evaluate_wait_for(wait_for, current_date)

        if is_ready_recheck:
            # All assets arrived — clean up all subscriptions, mark ready, return
            for asset_spec in missing_assets:
                _delete_subscription(asset_spec['name'], task_name)
            _mark_assets_ready(execution_name, missing_names)
            return {
                'type': 'asset_deps',
                'ready': True,
                'assets': asset_results_recheck
            }
        elif is_pure_and:
            partial = _handle_partial_recheck(
                execution_name, task_name, missing_assets, asset_results_recheck
            )
            if partial is not None:
                return partial

    if is_ready:
        # Assets were already ready at registration time — mark the task record
        # so evaluate_deps (called later when task_deps complete via
        # notify_dependents) knows assets were satisfied. Without this the task
        # would hang: evaluate_deps would see wait_for + assets_ready=false and
        # return verdict='wait', but no async asset arrival will ever trigger
        # notify_asset_subscribers to flip the flag (there's no subscription).
        all_names = [r['name'] for r in asset_results]
        _mark_assets_ready(execution_name, all_names)

    return {
        'type': 'asset_deps',
        'ready': is_ready,
        'assets': asset_results
    }


def _mark_assets_ready(execution_name: str, asset_names: List[str]) -> None:
    """Best-effort: mark all wait_for assets as satisfied on the task record.
    Failure here doesn't block registration — the task's wait_for path still
    functions via the immediate registration signal — but a downstream restart
    or a concurrent task_deps completion would then not see the flag."""
    if not execution_name:
        return
    try:
        tokens_repo.mark_assets_ready(execution_name, asset_names)
    except Exception as e:
        log.error("_mark_assets_ready", "mark_assets_ready failed — evaluate_deps may not see assets as ready",
                  execution_name=execution_name, error=str(e))


def _write_pending_assets(execution_name: str, asset_names: List[str]) -> None:
    """Best-effort: write `wait_for_assets` = subscribed names on the task
    record so notify_asset_subscribers can gate the wake-up signal on ALL
    assets arriving (AND semantics). A failure here degrades to the legacy
    behaviour where the first arriving asset wakes the task."""
    if not execution_name or not asset_names:
        return
    try:
        tokens_repo.write_pending_assets(execution_name, asset_names)
    except Exception as e:
        log.error("_write_pending_assets", "write failed — AND semantics degraded to OR",
                  execution_name=execution_name, error_type=type(e).__name__, error=str(e))


def _handle_partial_recheck(
    execution_name: str,
    task_name: str,
    missing_assets: List[Dict],
    asset_results_recheck: List[Dict],
) -> Optional[Dict]:
    """Credit assets that arrived during the subscription-save window and check
    if the AND gate is now complete. Returns a ready=True response dict when the
    gate closes, None when still waiting (subscriptions stay active for the rest).

    Write order: ADD first, delete subscriptions after. If the ADD fails,
    subscriptions remain — a known-incomplete set is better than no recovery path.
    """
    missing_names_set = {spec['name'] for spec in missing_assets}
    newly_ready_names = [
        r['name'] for r in asset_results_recheck
        if r.get('ready') and r['name'] in missing_names_set
    ]
    if not newly_ready_names:
        return None

    updated = _credit_arrived_assets(execution_name, newly_ready_names)
    if updated is None:
        return None

    for name in newly_ready_names:
        _delete_subscription(name, task_name)

    ready_set = updated.get('assets_ready_set') or set()
    gate_set = updated.get('wait_for_assets') or set()
    if not (gate_set and ready_set >= gate_set):
        return None

    # Gate complete — clean up remaining subscriptions and wake task.
    # The ADD is atomic: at most one side (notify or this path) observes
    # the gate flip and acts on it; a concurrent double-signal is safe
    # (the registration SFN Catch handles an already-consumed token).
    newly_ready_set = set(newly_ready_names)
    for spec in missing_assets:
        if spec['name'] not in newly_ready_set:
            _delete_subscription(spec['name'], task_name)
    return {
        'type': 'asset_deps',
        'ready': True,
        'assets': asset_results_recheck
    }


def _credit_arrived_assets(execution_name: str, asset_names: List[str]) -> Optional[Dict]:
    """Atomically ADD already-arrived asset names to assets_ready_set, return
    the updated record. The caller checks whether the gate is now complete.
    Returns None on failure (caller keeps subscriptions active as a safeguard)."""
    if not execution_name or not asset_names:
        return None
    try:
        return tokens_repo.add_assets_to_ready_set_and_get(execution_name, asset_names)
    except Exception as e:
        log.error("_credit_arrived_assets", "ADD to assets_ready_set failed — AND gate may not close",
                  execution_name=execution_name, error_type=type(e).__name__, error=str(e))
        return None


def _has_or_group(wait_for: List[Dict]) -> bool:
    """Return True if any item in wait_for (recursively) uses OR semantics.

    Used to decide whether the AND-completeness gate applies: OR groups need
    first-arrival semantics (any one qualifying asset wakes the task), not
    all-must-arrive semantics. See ADR #129."""
    for item in wait_for:
        if item.get('operator') == 'OR':
            return True
        if 'assets' in item and _has_or_group(item['assets']):
            return True
    return False


def _evaluate_wait_for(wait_for: List[Dict], current_date: str = '') -> tuple:
    """
    Evaluate wait_for list with AND/OR logic.
    
    Default (list) = AND: all must be ready
    OR operator = any one is enough
    Supports: asset (latest), freshness_hours, consecutive_days
    
    Returns: (is_ready: bool, asset_results: List[Dict])
    """
    # Check if this is a single OR/AND group at top level
    if len(wait_for) == 1 and 'operator' in wait_for[0]:
        return _evaluate_group(wait_for[0], current_date)
    
    # Default: treat as AND (all items must be ready)
    all_results = []
    all_ready = True
    
    for item in wait_for:
        if 'operator' in item:
            # Nested group
            group_ready, group_results = _evaluate_group(item, current_date)
            all_results.extend(group_results)
            if not group_ready:
                all_ready = False
        elif 'asset_name' in item:
            if 'consecutive_days' in item:
                # Consecutive check
                result = _check_asset_consecutive(
                    item['asset_name'],
                    item['consecutive_days'],
                    current_date
                )
            else:
                # Single asset (latest or freshness)
                result = _check_asset(item['asset_name'], item.get('freshness_hours'))
            all_results.append(result)
            if not result['ready']:
                all_ready = False
    
    return all_ready, all_results


def _evaluate_group(group: Dict, current_date: str = '') -> tuple:
    """
    Evaluate AND/OR group.
    
    Returns: (is_ready: bool, asset_results: List[Dict])
    """
    operator = group.get('operator', 'AND')
    assets = group.get('assets', [])
    
    results = []
    ready_count = 0
    
    for item in assets:
        if 'operator' in item:
            # Nested group
            nested_ready, nested_results = _evaluate_group(item, current_date)
            results.extend(nested_results)
            if nested_ready:
                ready_count += 1
        elif 'asset_name' in item:
            if 'consecutive_days' in item:
                result = _check_asset_consecutive(
                    item['asset_name'],
                    item['consecutive_days'],
                    current_date
                )
            else:
                result = _check_asset(item['asset_name'], item.get('freshness_hours'))
            results.append(result)
            if result['ready']:
                ready_count += 1
    
    if operator == 'OR':
        # OR: at least one must be ready
        is_ready = ready_count > 0
    else:
        # AND: all must be ready
        is_ready = ready_count == len(assets)
    
    return is_ready, results


def _get_missing_assets(asset_results: List[Dict]) -> List[Dict]:
    """Get list of assets that are not ready."""
    missing = []
    for result in asset_results:
        if not result.get('ready', False):
            spec = {
                'name': result['name'],
                'freshness_hours': result.get('freshness_hours')
            }
            if 'consecutive_days' in result:
                spec['consecutive_days'] = result['consecutive_days']
            missing.append(spec)
    return missing


def _check_asset_consecutive(asset_name: str, consecutive_days: int, reference_date: str) -> Dict[str, Any]:
    """
    Check if asset has events for N consecutive dates ending at reference_date.
    
    Queries last 30 events (to handle duplicates), extracts distinct execution_dates,
    then checks if all dates from (reference_date - N+1) to reference_date are present.
    
    Returns:
        {
            "name": "acme/daily-complete",
            "ready": true|false,
            "consecutive_days": 7,
            "found_dates": ["2026-02-16", ..., "2026-02-22"],
            "missing_dates": ["2026-02-20"],  # if not ready
            "reason": "consecutive_incomplete"  # if not ready
        }
    """
    from datetime import timedelta
    
    # Defensive: consecutive_days must be >= 1
    if consecutive_days < 1:
        return {
            'name': asset_name,
            'ready': False,
            'consecutive_days': consecutive_days,
            'reason': f'invalid_consecutive_days: {consecutive_days}'
        }
    
    # Calculate required date range
    if not reference_date:
        reference_date = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    try:
        ref_dt = datetime.strptime(reference_date, '%Y-%m-%d')
    except ValueError:
        return {
            'name': asset_name,
            'ready': False,
            'consecutive_days': consecutive_days,
            'reason': f'invalid_reference_date: {reference_date}'
        }
    
    required_dates = set()
    for i in range(consecutive_days):
        dt = ref_dt - timedelta(days=i)
        required_dates.add(dt.strftime('%Y-%m-%d'))
    
    # Query recent events (v0.79.3 — via DAL)
    try:
        items = asset_events_repo.query_recent(
            asset_name, limit=max(consecutive_days * 5, 30)
        )
    except Exception as e:
        log.error("_check_consecutive", "Failed to query events", asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return {
            'name': asset_name,
            'ready': False,
            'consecutive_days': consecutive_days,
            'reason': f'error: {str(e)}'
        }
    
    # Extract distinct execution_dates
    found_dates = set()
    for item in items:
        exec_date = item.get('execution_date', '')
        if exec_date:
            found_dates.add(exec_date)
    
    missing_dates = sorted(required_dates - found_dates)
    found_sorted = sorted(required_dates & found_dates)
    
    if not missing_dates:
        return {
            'name': asset_name,
            'ready': True,
            'consecutive_days': consecutive_days,
            'found_dates': found_sorted
        }
    
    return {
        'name': asset_name,
        'ready': False,
        'consecutive_days': consecutive_days,
        'found_dates': found_sorted,
        'missing_dates': missing_dates,
        'reason': 'consecutive_incomplete'
    }


def _check_asset(asset_name: str, freshness_hours: Optional[int]) -> Dict[str, Any]:
    """
    Check if asset has a recent event.
    
    Returns:
        {
            "name": "inventory",
            "ready": true|false,
            "event_time": "2026-01-26T08:00:00Z" (if ready),
            "uri": "s3://..." (if ready),
            "reason": "no_event"|"stale" (if not ready)
        }
    """
    # v0.79.3 (ADR #75) — DAL: get_latest returns Optional[Dict].
    # asset_events table: PK=asset_name, SK=event_time (sorted desc)
    try:
        latest_item = asset_events_repo.get_latest(asset_name)
    except Exception as e:
        log.error("_check_asset", "Failed to query latest event", asset_name=asset_name, error_type=type(e).__name__, error=str(e))
        return {
            'name': asset_name,
            'ready': False,
            'reason': f'error: {str(e)}'
        }

    if latest_item is None:
        return {
            'name': asset_name,
            'ready': False,
            'reason': 'no_event'
        }

    latest = latest_item
    event_time = latest.get('event_time', '')
    uri = latest.get('uri', '')
    
    # Check freshness if specified
    if freshness_hours is not None and freshness_hours > 0:
        if not _is_fresh(event_time, freshness_hours):
            return {
                'name': asset_name,
                'ready': False,
                'reason': 'stale',
                'event_time': event_time,
                'freshness_hours': freshness_hours
            }
    
    return {
        'name': asset_name,
        'ready': True,
        'event_time': event_time,
        'uri': uri
    }


def _is_fresh(event_time: str, freshness_hours: int) -> bool:
    """Check if event_time is within freshness_hours of now."""
    try:
        # Parse ISO format
        if 'Z' in event_time:
            event_time = event_time.replace('Z', '+00:00')
        event_dt = datetime.fromisoformat(event_time)
        
        # Make timezone-aware if needed
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        age_hours = (now - event_dt).total_seconds() / 3600
        
        return age_hours <= freshness_hours
    except Exception as e:
        # "Not fresh" on parse failure is the safe default (caller will wait for
        # the next event rather than proceeding on a bad timestamp). ERROR
        # level (not WARN) so a systematic timestamp format issue — which
        # would stall every pipeline reading this asset — is visible in
        # CloudWatch without digging.
        log.error("_is_fresh", "Parse failed; defaulting to not fresh",
                  event_time=event_time,
                  error_type=type(e).__name__,
                  error=str(e))
        return False


def _save_asset_subscription(
    asset_name: str,
    subscriber: str,
    wait_token: str,
    execution_name: str,
    freshness_hours: Optional[int],
    ttl: int,
    consecutive_days: Optional[int] = None,
    reference_date: str = ''
) -> None:
    """
    Save subscription for an asset.
    
    Key format: asset:{asset_name}
    This allows notify to find all subscribers waiting for this asset.
    """
    # v0.79.3 (ADR #75) — DAL handles item construction + put_item.
    try:
        subscriptions_repo.put_asset_subscription(
            asset_name=asset_name,
            subscriber=subscriber,
            wait_token=wait_token,
            execution_name=execution_name,
            ttl=ttl,
            freshness_hours=freshness_hours,
            consecutive_days=consecutive_days,
            reference_date=reference_date,
        )
    except Exception as e:
        # Subscription write is the critical path that makes wait_for work when
        # the asset has not been materialized yet. If this fails the task will
        # go into Wait_For_Signal and hang to orchestration_timeout — no
        # subscriber exists to be woken by the eventual asset event. Logged at
        # ERROR (not WARN) so this is visible in CloudWatch without digging —
        # the swallowed warn is what hid the DDB schema mismatch
        # (ValidationException from writing dependency_key to a table keyed
        # on asset_name) for months.
        log.error(
            "_save_subscription",
            "put_item failed — task will hang to timeout; no subscription persisted",
            asset_name=asset_name, subscriber=subscriber,
            error_type=type(e).__name__, error=str(e),
        )


def _delete_subscription(asset_name: str, subscriber: str) -> None:
    """Delete subscription after race condition resolution."""
    try:
        subscriptions_repo.delete(asset_name, subscriber)
    except Exception as e:
        # WARN (not ERROR) because this is cleanup, not a wake-up path: the
        # task already proceeded via _mark_assets_ready right above. A stale
        # subscription row is harmless — TTL will reap it, and
        # notify_asset_subscribers filters by task state before signaling.
        # The "catches on wake-up path log at error" rule in polyris/CLAUDE.md
        # explicitly scopes to wake-up; this is post-wake-up housekeeping.
        log.warn("_delete_subscription", "delete_item failed", asset_name=asset_name, subscriber=subscriber, error=str(e))
