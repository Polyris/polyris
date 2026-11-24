import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve } from 'path';

import {
    RECENT_WINDOW_DAYS, RUNS_WINDOW_DAYS, historyDatePlaceholder, historyEmptyState, isHistoryWindowed,
} from './historyWindow';

/** A `NAME = N` constant of console_api/constants.py; process.cwd() is ui/ under vitest. */
function consoleConstant(name: string): number {
    const source = readFileSync(resolve(process.cwd(), '../sam/lambdas/console_api/constants.py'), 'utf-8');
    const match = source.match(new RegExp(`^\\s*${name}\\s*=\\s*(\\d+)`, 'm'));
    expect(match, `${name} not found in console_api/constants.py`).not.toBeNull();
    return Number(match![1]);
}

describe('History windows', () => {
    it('are the console API\'s windows (Limits.RUNS_WINDOW_DAYS, Limits.SLA_DAYS) — Principle #28', () => {
        expect(RUNS_WINDOW_DAYS).toBe(consoleConstant('RUNS_WINDOW_DAYS'));
        expect(RECENT_WINDOW_DAYS).toBe(consoleConstant('SLA_DAYS'));
    });

    it('apply to both tabs without a date, with a pipeline or without: both read run rows (D140)', () => {
        for (const noun of ['runs', 'tasks'] as const) {
            expect(isHistoryWindowed(noun, { pipeline: '', date: '' })).toBe(true);
            expect(isHistoryWindowed(noun, { pipeline: 'sales', date: '' })).toBe(true);
            expect(isHistoryWindowed(noun, { pipeline: '', date: '2026-11-14' })).toBe(false);
        }
    });

    it('are named in the date filter and the empty state', () => {
        expect(historyDatePlaceholder('runs', true)).toBe(`Last ${RUNS_WINDOW_DAYS} days`);
        expect(historyDatePlaceholder('tasks', true)).toBe(`Last ${RUNS_WINDOW_DAYS} days`);
        expect(historyDatePlaceholder('runs', false)).toBe('All dates');
        expect(historyEmptyState('runs', false, true)).toEqual({
            title: `No runs in the last ${RUNS_WINDOW_DAYS} days`, description: 'To see older runs, pick a date.',
        });
        expect(historyEmptyState('runs', true, true).description)
            .toContain(`Without a date, only the last ${RUNS_WINDOW_DAYS} days`);
        expect(historyEmptyState('tasks', true, true).description)
            .toContain(`Without a date, only the last ${RUNS_WINDOW_DAYS} days`);
        expect(historyEmptyState('tasks', true, false).description).toBe('Try adjusting your filters or search');
    });
});
