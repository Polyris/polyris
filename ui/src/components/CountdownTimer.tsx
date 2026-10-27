import React, { useState, useEffect } from 'react';
import { formatCountdown, computeWaitCountdown, isInRetryWait } from '../utils';
import { Hourglass, Timer, Check } from '@/utils/icons';
import { TASK_STATUS, isTerminalStatus, MS } from '@/utils/constants';

/**
 * CountdownTimer - displays wait_before countdown in task details panel.
 * 
 * Uses unified computeWaitCountdown() utility for countdown logic.
 * Has its own local tick to avoid re-rendering parent components.
 */
export function CountdownTimer({ 
    waitBefore,
    waitDelayUntilMs,
    waitDelayStartedMs,
    status,
    serverOffsetMs
}: { waitBefore: number | null; waitDelayUntilMs: number | null; waitDelayStartedMs: number | null; status: string; serverOffsetMs: number }) {
    // Local tick - only this component re-renders every second
    const [tick, setTick] = useState(() => Date.now());

    // Compute now first so isRetryWaiting can guard the early-return below
    const nowMs = tick + (serverOffsetMs || 0);
    const isRetryWaiting = isInRetryWait(status, waitDelayUntilMs, nowMs);

    // Use centralized status check (STOPPED is not terminal but should stop countdown)
    const isTerminal = isTerminalStatus(status) || status === TASK_STATUS.STOPPED;

    // Tick when actively counting down: initial wait_before delay OR in-SFN retry wait
    const isActivelyCountingDown = status === TASK_STATUS.WAITING_DELAY
        || (status === TASK_STATUS.RUNNING && !!waitDelayUntilMs);

    useEffect(() => {
        if (!isActivelyCountingDown || isTerminal) return;
        // Tick for retry wait even when task has no wait_before configured
        if (!waitBefore && !isRetryWaiting) return;
        const interval = setInterval(() => setTick(Date.now()), MS.TICK_INTERVAL);
        return () => clearInterval(interval);
    }, [isActivelyCountingDown, waitBefore, isRetryWaiting, isTerminal]);

    // Show for retry wait even when task has no wait_before configured
    if (isTerminal || ((!waitBefore || waitBefore <= 0) && !isRetryWaiting)) return null;
    
    // Use unified countdown computation (waitBefore=0 is valid when isRetryWaiting)
    const countdown = computeWaitCountdown({
        status,
        waitBefore: waitBefore ?? 0,
        waitDelayUntilMs: waitDelayUntilMs ?? undefined,
        waitDelayStartedMs: waitDelayStartedMs ?? undefined,
        nowMs
    });
    
    // Don't render if no countdown state
    const isDepsReady = status === TASK_STATUS.DEPS_READY;
    if (!countdown.isCountingDown && !countdown.isCompleted && !isDepsReady) {
        return null;
    }
    
    const { remainingSeconds, isCountingDown, isCompleted, progressPercent } = countdown;
    
    return (
        <div className={`detail-section ct-wrapper ${isCountingDown ? 'ct-wrapper--active' : 'ct-wrapper--idle'}`} role="timer" aria-live="polite" aria-label={isCountingDown ? `${formatCountdown(remainingSeconds)} remaining` : `Wait before: ${formatCountdown(waitBefore)}`}>
            <div className={`detail-label flex items-center gap-1.5 ${isCountingDown ? 'ct-label--active' : 'ct-label--idle'}`}>
                {isCountingDown
                    ? <Hourglass size={14} className="text-amber-500" />
                    : <Timer size={14} className="text-indigo-500" />
                }
                <span>{isCountingDown
                    ? (isRetryWaiting ? 'Waiting to retry' : 'Waiting to Start')
                    : 'Wait Before Start'
                }</span>
            </div>
            <div className={`detail-value flex items-center gap-2 ct-value ${isCountingDown ? 'ct-label--active' : 'ct-label--idle'}`}>
                {isCountingDown
                    ? `${formatCountdown(remainingSeconds)} remaining`
                    : isCompleted
                        ? (
                            <>
                                <Check size={20} className="text-green-500" />
                                {formatCountdown(waitBefore ?? 0)} waited
                            </>
                          )
                        : formatCountdown(waitBefore ?? 0)
                }
            </div>
            <div className="text-xs text-muted mt-sm">
                {isDepsReady
                    ? 'Waiting for delay to start...'
                    : isCountingDown
                        ? (isRetryWaiting ? 'Task will retry after countdown' : 'Task will start after countdown')
                        : isCompleted
                            ? 'Wait completed'
                            : 'This task waits before executing'
                }
            </div>
            {isCountingDown && (
                <div className="ct-progress-track">
                    <div className="ct-progress-fill" style={{ width: `${progressPercent}%` }} />
                </div>
            )}
        </div>
    );
}

export default CountdownTimer;
