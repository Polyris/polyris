/**
 * Unified countdown computation for wait_before delays.
 * 
 * Single source of truth for countdown logic used by:
 * - CountdownTimer.jsx (task details panel)
 * - DAGGraphFlow.jsx (graph node badges)
 * 
 * Handles three scenarios:
 * 1. waiting_delay with deadline (wait_delay_until_ms) - count down from deadline
 * 2. waiting_delay with start time only - compute remaining from elapsed
 * 3. deps_ready - show full wait_before (countdown not started yet, isPending=true)
 */

import { TASK_SETTLED_STATUSES } from '@/generated/enums';

/**
 * Compute countdown state for a task's wait_before delay.
 * 
 * @param {Object} params
 * @param {string} params.status - Task status (waiting_delay, deps_ready, running, etc.)
 * @param {number} params.waitBefore - Total wait duration in seconds
 * @param {number} params.waitDelayUntilMs - Deadline timestamp in ms (when wait ends)
 * @param {number} params.waitDelayStartedMs - Start timestamp in ms (when wait began)
 * @param {number} params.nowMs - Current time in ms (server-adjusted)
 * @returns {Object} { remainingSeconds, isCountingDown, isCompleted, isPending, progressPercent }
 */
interface CountdownParams {
    status: string;
    waitBefore: number;
    waitDelayUntilMs?: number;
    waitDelayStartedMs?: number;
    nowMs: number;
}

interface CountdownResult {
    remainingSeconds: number;
    isCountingDown: boolean;
    isCompleted: boolean;
    isPending: boolean;
    progressPercent: number;
}

export function computeWaitCountdown({
    status,
    waitBefore,
    waitDelayUntilMs,
    waitDelayStartedMs,
    nowMs
}: CountdownParams): CountdownResult {
    // Default result
    const result = {
        remainingSeconds: 0,
        isCountingDown: false,
        isCompleted: false,
        isPending: false,  // True when countdown will start but hasn't yet (deps_ready)
        progressPercent: 0
    };
    
    // No wait_before configured
    if (!waitBefore || waitBefore <= 0) {
        return result;
    }
    
    const isWaitingDelay = status === 'waiting_delay';
    const isDepsReady = status === 'deps_ready';
    const isTerminal = TASK_SETTLED_STATUSES.includes(status) || status === 'running';
    
    if (isWaitingDelay) {
        // Priority 1: Use deadline if available
        if (waitDelayUntilMs && waitDelayUntilMs > 0) {
            result.remainingSeconds = Math.max(0, Math.floor((waitDelayUntilMs - nowMs) / 1000));
            result.isCountingDown = result.remainingSeconds > 0;
        }
        // Priority 2: Compute from start time (fallback)
        else if (waitDelayStartedMs && waitDelayStartedMs > 0) {
            const elapsedSeconds = Math.floor((nowMs - waitDelayStartedMs) / 1000);
            result.remainingSeconds = Math.max(0, waitBefore - elapsedSeconds);
            result.isCountingDown = result.remainingSeconds > 0;
        }
        // Priority 3: No timing data yet, show full duration (edge case)
        else {
            result.remainingSeconds = waitBefore;
            result.isCountingDown = true;
        }
    } else if (isDepsReady) {
        // Dependencies ready, waiting to start delay - show full time as pending
        result.remainingSeconds = waitBefore;
        result.isPending = true;  // Countdown will start when deps complete
        result.isCountingDown = false;  // Not actively counting yet
    } else if (isTerminal) {
        // Task completed - show that wait was done
        result.remainingSeconds = 0;
        result.isCompleted = true;
    }
    
    // Calculate progress (for progress bar)
    if (waitBefore > 0) {
        if (result.isCountingDown) {
            // Active countdown - show remaining percentage
            result.progressPercent = Math.min(100, (result.remainingSeconds / waitBefore) * 100);
        } else if (result.isPending) {
            // Pending state - show full bar (100%) to indicate countdown hasn't started
            result.progressPercent = 100;
        }
    }
    
    return result;
}

/**
 * Format countdown for display in DAG graph badges.
 * 
 * Returns structured data instead of emoji strings.
 * The consuming component should use Lucide icons for rendering.
 * 
 * @param {Object} params - Same as computeWaitCountdown
 * @param {Function} formatFn - Formatting function (formatCountdown from formatters.js)
 * @returns {Object|null} Structured countdown data or null
 *   - { type: 'countdown', text: '5m 30s', icon: 'hourglass' }
 *   - { type: 'complete', text: '1m', icon: 'check' }
 */
interface FormatWaitBadgeParams extends CountdownParams {
    formatFn: (seconds: number | string | null | undefined) => string | null;
}

interface WaitBadgeResult {
    type: 'countdown' | 'pending' | 'complete';
    text: string;
    icon: 'hourglass' | 'clock' | 'check';
}

export function formatWaitBadge({
    status,
    waitBefore,
    waitDelayUntilMs,
    waitDelayStartedMs,
    nowMs,
    formatFn
}: FormatWaitBadgeParams): WaitBadgeResult | null {
    if (!waitBefore || waitBefore <= 0) {
        return null;
    }
    
    const countdown = computeWaitCountdown({
        status,
        waitBefore,
        waitDelayUntilMs,
        waitDelayStartedMs,
        nowMs
    });
    
    if (countdown.isCountingDown) {
        return {
            type: 'countdown',
            text: formatFn(countdown.remainingSeconds) || "",
            icon: 'hourglass'
        };
    } else if (countdown.isPending) {
        // Pending state - show clock icon to indicate countdown will start
        return {
            type: 'pending',
            text: formatFn(countdown.remainingSeconds) || "",
            icon: 'clock'
        };
    } else if (countdown.isCompleted) {
        return {
            type: 'complete',
            text: formatFn(waitBefore) || "",
            icon: 'check'
        };
    }
    
    return null;
}
