import { describe, it, expect } from 'vitest';
import {
    detectManualResolution,
    formatManualResolution,
    statusForResolution,
    variantForResolution,
} from './manualResolution';

describe('detectManualResolution', () => {
    it('returns null for non-marker outputs (dict, list, primitive, null, undefined)', () => {
        expect(detectManualResolution({ rows: 42 })).toBeNull();
        expect(detectManualResolution([1, 2, 3])).toBeNull();
        expect(detectManualResolution(42)).toBeNull();
        expect(detectManualResolution('string')).toBeNull();
        expect(detectManualResolution(null)).toBeNull();
        expect(detectManualResolution(undefined)).toBeNull();
    });

    it('returns null when _manually_resolved is missing or false', () => {
        expect(detectManualResolution({ _manually_resolved: false })).toBeNull();
        expect(detectManualResolution({ _resolution: 'skip' })).toBeNull();
    });

    it('extracts resolution, reason, operator from a full marker', () => {
        const m = detectManualResolution({
            _manually_resolved: true,
            _resolution: 'mark_success',
            _reason: 'verified via S3',
            _operator: 'alice@example.com',
        });
        expect(m).toEqual({
            resolution: 'mark_success',
            reason: 'verified via S3',
            operator: 'alice@example.com',
        });
    });

    it('falls back to "operator" when _operator is missing (pre-0.100.0 records)', () => {
        const m = detectManualResolution({
            _manually_resolved: true,
            _resolution: 'skip',
            _reason: 'legacy row',
        });
        expect(m?.operator).toBe('operator');
    });

    it('falls back to "unknown" resolution and empty reason on malformed fields', () => {
        const m = detectManualResolution({
            _manually_resolved: true,
            _resolution: 42,
            _reason: null,
        });
        expect(m?.resolution).toBe('unknown');
        expect(m?.reason).toBe('');
    });
});

describe('formatManualResolution', () => {
    it('formats each resolution verb correctly', () => {
        const base = { operator: 'alice', reason: 'because' };
        expect(formatManualResolution({ ...base, resolution: 'mark_success' }))
            .toBe('Marked success by alice — because');
        expect(formatManualResolution({ ...base, resolution: 'skip' }))
            .toBe('Skipped by alice — because');
        expect(formatManualResolution({ ...base, resolution: 'fail' }))
            .toBe('Marked failed by alice — because');
        expect(formatManualResolution({ ...base, resolution: 'stop' }))
            .toBe('Stopped by alice — because');
    });

    it('omits the reason clause when reason is empty', () => {
        expect(formatManualResolution({ resolution: 'skip', operator: 'alice', reason: '' }))
            .toBe('Skipped by alice');
    });

    it('falls back for unknown resolutions', () => {
        expect(formatManualResolution({ resolution: 'weird', operator: 'x', reason: '' }))
            .toBe('Resolved (weird) by x');
    });
});

describe('statusForResolution', () => {
    it('maps each known resolution to its DDB task-status equivalent', () => {
        expect(statusForResolution('mark_success')).toBe('success');
        expect(statusForResolution('skip')).toBe('skipped');
        expect(statusForResolution('fail')).toBe('failed');
        expect(statusForResolution('stop')).toBe('stopped');
    });

    it('echoes unknown resolutions verbatim so the badge is never blank', () => {
        expect(statusForResolution('custom_action')).toBe('custom_action');
    });
});

describe('variantForResolution', () => {
    it('mark_success stays green (success), skip/fail red (error), stop muted', () => {
        expect(variantForResolution('mark_success')).toBe('success');
        expect(variantForResolution('skip')).toBe('error');
        expect(variantForResolution('fail')).toBe('error');
        expect(variantForResolution('stop')).toBe('muted');
    });

    it('defaults unknown resolutions to error (least-surprise for surprising input)', () => {
        expect(variantForResolution('anything_else')).toBe('error');
    });
});
