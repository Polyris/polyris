"""
Evaluate Dependencies Lambda

Minimal Lambda for operations Step Functions cannot do natively:
1. BatchGetItem - fetch all dependency statuses in one call
2. Evaluate trigger_rule - complex logic (5 rule names, ADR #115/#117)
3. Check pipeline paused status

Called from notify_dependents SFN helper.

Input:
{
    "dependencies": ["task_a", "task_b"],
    "trigger_rule": "all_success",
    "date": "2026-01-19",
    "pipeline_execution_short": "abc123",
    "pipeline_execution": "pipeline-2026-01-19-abc123"
}

Output:
{
    "is_ready": true,
    "is_blocked": false,
    "is_paused": false,
    "verdict": "ready",
    "reason": "all_success",
    "dep_statuses": ["success", "success"],
    "counts": {
        "total": 2,
        "success": 2,
        "failed": 0,
        "skipped": 0,
        "pending": 0
    }
}

verdict (ADR #115) is one of:
- "ready": deps_satisfied and not paused (or would-be-ready but currently paused)
- "wait": not all deps are terminal yet
- "skip": all deps terminal, rule not satisfied, but no real failure caused it —
  the rule's trigger condition simply never occurred (legitimate no-op)
- "upstream_failed": all deps terminal, rule not satisfied, and a real failure on a
  success/no-failure-requiring rule caused the block (genuine upstream problem)
"""

import os
from typing import List, Dict, Tuple, Any, Optional

from constants import (TASK_TERMINAL_STATUSES, TASK_SUCCESS_STATUSES, TASK_FAILURE_STATUSES)
# v0.79.3 (ADR #75) — DAL repository pattern for all DynamoDB access.
# All raw boto3 calls now live in dal/__init__.py; tests mock this repo
# instead of patching boto3 directly.
from dal import tokens_repo
# v0.79.4 (ADR #76) — structured JSON logging via shared logger module
# (copied to each Lambda by sync_loggers Make target; canonical source
# in sam/lambdas/_shared/logger.py).
from logger import log

TOKENS_TABLE = os.environ.get('TOKENS_TABLE', 'pipeline-tokens')

# Status categories - use centralized definitions
TERMINAL_SUCCESS = TASK_SUCCESS_STATUSES
# The "ran and succeeded" aliases only — TERMINAL_SUCCESS also bundles 'skipped',
# which is counted separately. Derived from the canonical set so the 'succeeded'
# alias (produced by normalize_execution_status from SFN 'SUCCEEDED') is included.
SUCCESS_ONLY = TERMINAL_SUCCESS - {'skipped'}
TERMINAL_FAILURE = TASK_FAILURE_STATUSES
TERMINAL_STATUSES = TASK_TERMINAL_STATUSES

# ADR #115/#117 — rules whose trigger condition *requires* success / absence-of-failure.
# When one of these is blocked (all deps terminal, rule not satisfied), the block is a
# genuine upstream problem only if a real failure is present among the deps; otherwise
# the block is caused by a skip and is not an error. Rules NOT in this set (all_done,
# all_skipped, none_skipped) never legitimately reach 'blocked' as a failure signal: their
# own trigger condition is about done/skip *counts*, not about requiring success, so a
# blocked verdict for them is always a no-op ('skip'), never 'upstream_failed'.
FAILURE_AVERSE_RULES = frozenset({
    'all_success',
    'one_success',
})


def handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Main handler - evaluate if dependencies satisfy trigger_rule."""
    try:
        return _handler(event, context)
    except Exception as e:
        log.error("handler", "Unhandled error", error_type=type(e).__name__, error=str(e), event=event)
        raise


def _handler(event: Dict[str, Any], context) -> Dict[str, Any]:
    """Inner handler."""
    dependencies = event.get('dependencies', [])
    trigger_rule = event.get('trigger_rule', 'all_success')
    date = event.get('date', '')
    pipeline_execution_short = event.get('pipeline_execution_short', '')
    pipeline_execution = event.get('pipeline_execution', '')
    # Asset coordination (Case B fix): task_deps and wait_for assets can each
    # arrive first. Whichever caller (notify_dependents SFN via a completed
    # task_dep, or notify_asset_subscribers via an arriving asset) evaluates
    # last should be the one that signals — so evaluate_deps is the single
    # gate that checks BOTH. Callers pass wait_for from the subscriber's task
    # record and assets_ready from the same record (set by
    # notify_asset_subscribers before the call).
    wait_for = event.get('wait_for', []) or []
    assets_ready = bool(event.get('assets_ready', False))
    has_wait_for = len(wait_for) > 0
    assets_satisfied = (not has_wait_for) or assets_ready

    # Handle empty dependencies
    if not dependencies:
        # No task_deps means task_deps trivially satisfied. But if the task has
        # wait_for assets that haven't arrived yet, the task is still not ready.
        return {
            'is_ready': assets_satisfied,
            'is_blocked': False,
            'is_paused': False,
            'deps_satisfied': True,  # No deps = satisfied by default
            'verdict': 'ready' if assets_satisfied else 'wait',
            'reason': 'no_deps' if assets_satisfied else 'awaiting_assets',
            'dep_statuses': [],
            'counts': _empty_counts()
        }
    
    # 1. BatchGetItem - SFN doesn't support this
    dep_statuses = _batch_get_dep_statuses(dependencies, date, pipeline_execution_short)
    
    # 2. Check if pipeline is paused
    is_paused = _is_pipeline_paused(pipeline_execution) if pipeline_execution else False
    
    # 2b. ADR #115 step 1.2: only all_success cares whether a 'skipped' dep was
    # rule-originated (cascades) vs manual/unknown (does not). Fetching skip_origin
    # is a second, small DDB round-trip, so only do it when a skip is actually
    # present — the common case (no skips) pays nothing extra.
    rule_originated_skip = None
    if 'skipped' in dep_statuses:
        rule_originated_skip = _batch_get_rule_originated_skip(
            dependencies, dep_statuses, date, pipeline_execution_short
        )

    # 3. Evaluate trigger rule
    deps_satisfied, reason, effective_rule = _check_trigger_rule(
        trigger_rule, dep_statuses, rule_originated_skip
    )
    
    # 4. Check if permanently blocked (all done but rule not satisfied)
    all_done = all(s in TERMINAL_STATUSES for s in dep_statuses)
    is_blocked = all_done and not deps_satisfied
    
    # 5. Calculate counts for observability
    counts = _calculate_counts(dep_statuses)

    # 6. Verdict for the blocked case (ADR #115). A blocked rule is a genuine upstream
    # problem — 'upstream_failed' — only when the rule requires success/no-failure
    # (FAILURE_AVERSE_RULES) AND a real failure is present. Otherwise the rule's trigger
    # condition simply never occurred (e.g. all_skipped with nothing skipped, or a
    # failure-averse rule blocked only by a skip) — that is a legitimate no-op, not an
    # error, and verdicts 'skip'. Not blocked -> 'ready' or 'wait'.
    if not is_blocked:
        if deps_satisfied:
            # Asset coordination (Case B): if task also has wait_for assets that
            # haven't arrived yet, hold the 'wait' verdict. notify_asset_subscribers
            # will re-evaluate when the asset arrives (via its own evaluate_deps
            # call) and signal at that point.
            verdict = 'ready' if assets_satisfied else 'wait'
        else:
            verdict = 'wait'
    elif effective_rule in FAILURE_AVERSE_RULES and counts['failed'] > 0:
        verdict = 'upstream_failed'
    else:
        verdict = 'skip'

    # is_ready = deps satisfied AND assets satisfied AND pipeline not paused
    # deps_satisfied = task_deps satisfy trigger_rule (regardless of assets/pause)
    return {
        'is_ready': deps_satisfied and assets_satisfied and not is_paused,
        'is_blocked': is_blocked,
        'is_paused': is_paused,
        'deps_satisfied': deps_satisfied,  # task_deps satisfy rule (even if assets/pause block)
        'assets_satisfied': assets_satisfied,  # wait_for assets ready (or task has none)
        'verdict': verdict,  # ADR #115: 'ready' | 'wait' | 'skip' | 'upstream_failed'
        'reason': reason,
        'dep_statuses': dep_statuses,
        'counts': counts
    }


def _batch_get_dep_statuses(
    dependencies: List[str], 
    date: str, 
    pipeline_execution_short: str
) -> List[str]:
    """
    Batch fetch dependency statuses using BatchGetItem.
    
    Returns statuses in same order as dependencies list.
    SFN doesn't support BatchGetItem, hence this Lambda.
    """
    if not dependencies:
        return []

    # v0.79.3 (ADR #75) — DAL repo replaces inline batch_get_item +
    # retry loop + fallback. The retry semantics are preserved verbatim
    # inside TokensRepo.batch_get_statuses.
    exec_names = [
        f"{dep}-{date}-{pipeline_execution_short}"
        for dep in dependencies
    ]
    results = tokens_repo.batch_get_statuses(exec_names)
    # Return statuses in original order
    return [results[name] for name in exec_names]


def _batch_get_rule_originated_skip(
    dependencies: List[str],
    dep_statuses: List[str],
    date: str,
    pipeline_execution_short: str,
) -> List[bool]:
    """
    For each dependency, True iff its status is 'skipped' AND its skip_origin is
    'rule' (ADR #115 step 1.2). Aligned with dependencies/dep_statuses by index.

    Only the skipped dependencies' execution_names are fetched — this narrows the
    (already sparse, best-effort) batch_get_skip_origins call to just what's needed.
    """
    skipped_exec_names = [
        f"{dep}-{date}-{pipeline_execution_short}"
        for dep, status in zip(dependencies, dep_statuses)
        if status == 'skipped'
    ]
    if not skipped_exec_names:
        return [False] * len(dependencies)

    origins = tokens_repo.batch_get_skip_origins(skipped_exec_names)
    return [
        status == 'skipped' and origins.get(f"{dep}-{date}-{pipeline_execution_short}") == 'rule'
        for dep, status in zip(dependencies, dep_statuses)
    ]


def _is_pipeline_paused(pipeline_execution: str) -> bool:
    """Check if pipeline execution is paused."""
    if not pipeline_execution:
        return False

    try:
        # v0.79.3 (ADR #75) — single DAL call; repo owns the pause-key
        # construction and the get_item shape.
        return tokens_repo.is_paused(pipeline_execution)
    except Exception as e:
        log.error("_is_pipeline_paused", "Failed to read pause flag", error_type=type(e).__name__, error=str(e), pipeline_execution=pipeline_execution)
        # Re-raise so SFN can catch and log — silently returning False would
        # cause tasks to start when pipeline is actually paused
        raise


def _check_trigger_rule(
    trigger_rule: str,
    dep_statuses: List[str],
    rule_originated_skip: Optional[List[bool]] = None,
) -> Tuple[bool, str, str]:
    """
    Evaluate if trigger_rule is satisfied.

    5 rule names (ADR #117): each produces a distinct, reachable behavior
    under polyris's intervention-first model. A *confirmed* failure (resolved
    via Fail) cancels the whole pipeline's Parallel before any downstream
    trigger_rule ever evaluates — so dep_statuses passed here never actually
    contains 'failed'/'upstream_failed' in practice. 6 rule names were
    rejected because, given that, they either duplicated one of these 5
    exactly (one_done/none_failed -> all_done;
    none_failed_min_one_success/all_done_min_one_success -> one_success) or
    could never be satisfied at all (all_failed/one_failed — their only use
    case is reacting to a confirmed failure). See docs/features/DSL.md for
    the full analysis.

    - all_success: All deps success/skipped (default)
    - one_success: At least one success (doesn't wait for all!)
    - all_done: All deps terminal (any status)
    - all_skipped: All deps skipped
    - none_skipped: No skips

    Returns: (is_satisfied, reason, effective_rule) — effective_rule is trigger_rule
    itself, except when trigger_rule is unrecognized, in which case it is 'all_success'
    (the rule actually evaluated). Callers needing to classify the blocked case (ADR
    #115) must use effective_rule, not the raw trigger_rule, so an unknown rule's
    silent all_success fallback is classified consistently with the logic that ran.

    rule_originated_skip (ADR #115, step 1.2): optional list aligned with
    dep_statuses by index, True where that dependency is 'skipped' *and* the
    skip was rule-originated (skip_origin='rule' — i.e. a downstream Auto_Skip
    from a trigger_rule whose condition never occurred). Only all_success uses
    this: a rule-originated skip no longer counts as 'ok' (it cascades — the
    whole point of ADR #115 decision 2), but a skip with no origin info (a
    manual skip, or any pre-existing/other skip with skip_origin absent) still
    counts as ok, preserving today's behavior (ADR #115 decision 5). Passing
    None (the default) is exactly equivalent to "every skip is non-cascading"
    — today's behavior, unchanged for every caller that doesn't fetch origins.
    """
    if not dep_statuses:
        return True, "no_deps", trigger_rule
    
    counts = _calculate_counts(dep_statuses)
    total = counts['total']
    success = counts['success']
    failed = counts['failed']
    skipped = counts['skipped']
    pending = counts['pending']
    # ADR #115: a rule-originated skip does not count toward all_success's "ok"
    # (it cascades); a skip with unknown/non-rule origin still does (unchanged
    # default). Every other rule below uses `skipped` directly and is
    # unaffected by rule_originated_skip.
    cascading_skips = sum(1 for x in (rule_originated_skip or []) if x)
    ok = success + (skipped - cascading_skips)  # success, or a non-cascading skip = OK
    
    rules = {
        'all_success': lambda: (
            (pending == 0 and ok == total, "all_success") if pending == 0 and ok == total
            else (False, f"waiting for {pending} deps" if pending > 0
                  else (f"{failed} failed" if failed > 0 else f"{cascading_skips} rule-skipped"))
        ),
        'one_success': lambda: (
            (True, "one_success") if success >= 1
            else (False, "waiting, no success yet" if pending > 0 else "none succeeded")
        ),
        'all_done': lambda: (
            (True, "all_done") if pending == 0
            else (False, f"waiting for {pending} deps")
        ),
        'all_skipped': lambda: (
            (pending == 0 and skipped == total, "all_skipped") if pending == 0 and skipped == total
            else (False, f"waiting for {pending} deps" if pending > 0 else f"not all skipped ({skipped}/{total})")
        ),
        'none_skipped': lambda: (
            (False, f"{skipped} deps skipped") if skipped > 0
            else ((True, "none_skipped") if pending == 0 else (False, f"waiting for {pending} deps"))
        ),
    }
    
    # ADR #117: removed aliases — map to their canonical equivalents so
    # pipelines deployed before the 11→5 trim keep correct runtime semantics
    # instead of silently falling back to all_success. Mirrors validation.py's
    # removed_rule_suggestions.
    rules['one_done'] = rules['all_done']
    rules['none_failed'] = rules['all_done']
    rules['none_failed_min_one_success'] = rules['one_success']

    if trigger_rule in rules:
        satisfied, reason = rules[trigger_rule]()
        return satisfied, reason, trigger_rule

    # Default to all_success for unknown rules
    log.warn("_check_trigger_rule", "Unknown trigger_rule; defaulting to all_success", trigger_rule=trigger_rule)
    satisfied, reason = rules['all_success']()
    return satisfied, reason, 'all_success'


def _calculate_counts(dep_statuses: List[str]) -> Dict[str, int]:
    """Calculate status counts for observability."""
    # 'succeeded' is the legacy alias for 'success' and is what
    # normalize_execution_status() produces from AWS SFN's 'SUCCEEDED'. Counting
    # only the literal 'success' silently dropped it, so all_success (the default
    # rule) would deadlock when a dependency reported 'succeeded'.
    success = sum(1 for s in dep_statuses if s in SUCCESS_ONLY)
    failed = sum(1 for s in dep_statuses if s in TERMINAL_FAILURE)
    skipped = sum(1 for s in dep_statuses if s == 'skipped')
    pending = sum(1 for s in dep_statuses if s not in TERMINAL_STATUSES)
    
    return {
        'total': len(dep_statuses),
        'success': success,
        'failed': failed,
        'skipped': skipped,
        'pending': pending
    }


def _empty_counts() -> Dict[str, int]:
    """Return empty counts structure."""
    return {
        'total': 0,
        'success': 0,
        'failed': 0,
        'skipped': 0,
        'pending': 0
    }
