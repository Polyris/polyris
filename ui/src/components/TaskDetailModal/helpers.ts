/**
 * TaskDetailModal helpers — trigger rule evaluation and status constants
 */

import { TASK_SETTLED_STATUSES } from '@/generated/enums';

// A dependency is "terminal" for trigger-rule evaluation once it is settled —
// already terminal, or deliberately stopped. Canonical source (generated from
// polyris); never re-list these statuses inline.
export const TERMINAL_STATUSES = TASK_SETTLED_STATUSES;

export function evaluateDepStatus(depStatus: string, triggerRule: string) {
    const isTerminal = TERMINAL_STATUSES.includes(depStatus);
    const isSuccess = depStatus === 'success' || depStatus === 'succeeded';
    const isSkipped = depStatus === 'skipped';
    
    switch (triggerRule) {
        case 'all_success': return isSuccess ? 'ok' : isTerminal ? 'blocked' : 'waiting';
        case 'all_done': return isTerminal ? 'ok' : 'waiting';
        case 'one_success': return isSuccess ? 'ok' : isTerminal ? 'neutral' : 'waiting';
        case 'none_skipped': return isSkipped ? 'blocked' : isTerminal ? 'ok' : 'waiting';
        case 'all_skipped': return isSkipped ? 'ok' : isTerminal ? 'blocked' : 'waiting';
        case 'always': return 'ok';
        default: return isSuccess ? 'ok' : isTerminal ? 'blocked' : 'waiting';
    }
}

export function getTriggerRuleExplanation(triggerRule: string) {
    const explanations: Record<string, string> = {
        'all_success': 'All dependencies must succeed',
        'all_done': 'All dependencies must complete (any status)',
        'one_success': 'At least one dependency must succeed',
        'none_skipped': 'No dependency can be skipped',
        'all_skipped': 'All dependencies must be skipped',
        'always': 'Runs regardless of dependency status',
    };
    return explanations[triggerRule] || `Rule: ${triggerRule}`;
}
