import { describe, it, expect } from 'vitest';
import { computeWaitCountdown, isCountingDownWait, waitUntilMs } from './countdown';
import { getWaitingReason } from './constants';

// A 1.3 task that counts down its wait_before, as /api/pipeline-status answers it (ADR #137 §1): the row stays
// `waiting` (1.3 has no `waiting_delay`), its upstreams have finished, and its countdown ends at `countdown_end`.
const NOW = Date.parse('2026-11-22T06:00:00.000Z');
const countingDown = {
    status: 'waiting', dependencies: ['extract'], wait_before: 3600,
    countdown_end: '2026-11-22T06:30:00.000Z', deadline: null,
};

describe('the countdown of a 1.3 row (review of TR-6d, P-3)', () => {
    it('ends at the row countdown_end, and at 1.2 wait_delay_until_ms when a row has one', () => {
        expect(waitUntilMs(countingDown)).toBe(Date.parse('2026-11-22T06:30:00.000Z'));
        expect(waitUntilMs({ wait_delay_until_ms: 1234, countdown_end: '2026-11-22T06:30:00.000Z' })).toBe(1234);
        expect(waitUntilMs({ countdown_end: 'not a time' })).toBeUndefined();
        expect(waitUntilMs(null)).toBeUndefined();
    });

    it('counts down a waiting row with a countdown end, and no waiting row without one', () => {
        const state = computeWaitCountdown({
            status: countingDown.status, waitBefore: countingDown.wait_before,
            waitDelayUntilMs: waitUntilMs(countingDown), nowMs: NOW,
        });
        expect(state.isCountingDown).toBe(true);
        expect(state.remainingSeconds).toBe(30 * 60);
        expect(isCountingDownWait('waiting', undefined)).toBe(false);
    });

    it('does not say it waits for an upstream that has finished', () => {
        expect(getWaitingReason(countingDown)).toBe('Counting down its wait_before');
        expect(getWaitingReason({ status: 'waiting', dependencies: ['extract'] })).toBe('Waiting for: extract');
    });
});
