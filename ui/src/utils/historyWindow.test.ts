import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

import { HISTORY_WINDOW_DAYS, historyDatePlaceholder, historyEmptyState, isHistoryWindowed } from './historyWindow';

describe('History window', () => {
    it('is the console API\'s window (Limits.SLA_DAYS) — Principle #28', () => {
        // process.cwd() is ui/ under vitest; the console API sits one level up.
        const source = readFileSync(resolve(process.cwd(), '../sam/lambdas/console_api/constants.py'), 'utf-8');
        const match = source.match(/^\s*SLA_DAYS\s*=\s*(\d+)/m);
        expect(match, 'SLA_DAYS not found in console_api/constants.py').not.toBeNull();
        expect(HISTORY_WINDOW_DAYS).toBe(Number(match![1]));
    });

    it('applies only without a pipeline and a date', () => {
        expect(isHistoryWindowed({ pipeline: '', date: '' })).toBe(true);
        expect(isHistoryWindowed({ pipeline: 'sales', date: '' })).toBe(false);
        expect(isHistoryWindowed({ pipeline: '', date: '2026-11-14' })).toBe(false);
    });

    it('names the window in the date filter and the empty state', () => {
        expect(historyDatePlaceholder(true)).toBe(`Last ${HISTORY_WINDOW_DAYS} days`);
        expect(historyDatePlaceholder(false)).toBe('All dates');
        expect(historyEmptyState('runs', false, true).title).toBe(`No runs in the last ${HISTORY_WINDOW_DAYS} days`);
        expect(historyEmptyState('tasks', true, true).description).toContain(`only the last ${HISTORY_WINDOW_DAYS} days`);
        expect(historyEmptyState('tasks', true, false).description).toBe('Try adjusting your filters or search');
    });
});
