import { describe, it, expect } from 'vitest';
import {
    formatBackfillError,
    backfillErrorToString,
} from './backfillErrors';
import { BACKFILL_ERROR_CODES } from '@/generated/enums';

describe('formatBackfillError', () => {
    it('maps known error code to user-friendly title + hint', () => {
        const info = formatBackfillError({
            error: 'range_too_large',
            message: '6000 partitions requested',
        });
        expect(info.title).toMatch(/exceeds 1000/i);
        expect(info.hint).toMatch(/Split/i);
    });

    it('maps concurrent_backfill_active with override hint', () => {
        const info = formatBackfillError({
            error: 'concurrent_backfill_active',
            message: 'Active backfill exists',
        });
        expect(info.title).toMatch(/already running/i);
        expect(info.hint).toMatch(/allow_concurrent/i);
    });

    it('falls back to backend message when code unknown', () => {
        const info = formatBackfillError({
            error: 'mystery_error',
            message: 'Something specific failed',
        });
        expect(info.title).toBe('Something specific failed');
        expect(info.hint).toBe('Error code: mystery_error');
    });

    it('handles Error instance with JSON-encoded body in message', () => {
        const err = new Error(JSON.stringify({
            error: 'invalid_partition_format',
            message: 'expected YYYY-MM-DD',
        }));
        const info = formatBackfillError(err);
        expect(info.title).toMatch(/Partition format/i);
        expect(info.hint).toMatch(/YYYY-MM-DD/);
    });

    it('handles plain Error instance', () => {
        const err = new Error('Network unreachable');
        const info = formatBackfillError(err);
        expect(info.title).toBe('Network unreachable');
    });

    it('handles totally unknown shape', () => {
        const info = formatBackfillError(null);
        expect(info.title).toBe('Backfill request failed');
    });

    it('granularity_override_not_allowed maps correctly', () => {
        const info = formatBackfillError({
            error: 'granularity_override_not_allowed',
        });
        expect(info.title).toMatch(/Granularity override not allowed/);
        expect(info.hint).toMatch(/ambiguous/i);
    });

    it('every backend error code has a friendly mapping (gated on the generated registry)', () => {
        // Replaces the old hand-maintained `criticalCodes` list, which silently
        // fell behind the backend (missing the v83 downstream / v81 upstream
        // codes, still listing dead ones) yet stayed green. BACKFILL_ERROR_CODES
        // is generated from polyris/constants.py and pinned to the route's
        // emitted literals by test_backfill_error_registry — so this assertion
        // fails the moment a new backend code lacks UI text (ADR #94).
        const unmapped: string[] = [];
        for (const code of BACKFILL_ERROR_CODES) {
            const info = formatBackfillError({ error: code });
            // Unmapped codes fall through to the generic fallback title.
            if (info.title === 'Backfill request failed') {
                unmapped.push(code);
            }
        }
        expect(unmapped).toEqual([]);
    });
});

describe('backfillErrorToString', () => {
    it('combines title and hint with em-dash', () => {
        const s = backfillErrorToString({ error: 'range_too_large' });
        expect(s).toMatch(/exceeds 1000/);
        expect(s).toMatch(/—/);
        expect(s).toMatch(/Split/);
    });

    it('returns just title when no hint', () => {
        const s = backfillErrorToString(new Error('Generic failure'));
        expect(s).toBe('Generic failure');
    });
});
