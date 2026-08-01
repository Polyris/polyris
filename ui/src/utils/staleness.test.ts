/**
 * Tests for `getAssetStaleness` (v0.75.5).
 *
 * Two resolution paths:
 *   1. Match in scoped `recentEvents` → use that.
 *   2. Otherwise fall back to `fallbackLastUpdated` (asset.last_updated
 *      from the lineage endpoint, independent of any date picker).
 *
 * The fallback fixes a v0.75.3 regression where any asset whose newest
 * event fell outside the picker's day rendered as if it had never run.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { getAssetStaleness } from './staleness';

// Pin "now" so relative-time labels in assertions are deterministic.
const NOW = new Date('2026-05-08T12:00:00Z');

beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
});

afterEach(() => {
    vi.useRealTimers();
});


describe('getAssetStaleness — path 1: recentEvents has match', () => {
    it('uses event_time from recentEvents when asset is present', () => {
        const events = [
            { asset_name: 'orders', event_time: '2026-05-08T10:00:00Z' },  // 2h ago
        ];
        const result = getAssetStaleness('orders', events);
        expect(result.status).toBe('fresh');
        expect(result.hours).toBeCloseTo(2, 0);
    });

    it('prefers recentEvents over fallback (path 1 wins)', () => {
        const events = [
            { asset_name: 'orders', event_time: '2026-05-08T10:00:00Z' },  // 2h ago
        ];
        const result = getAssetStaleness('orders', events, {
            // Older fallback should not override the live event.
            fallbackLastUpdated: '2026-05-01T10:00:00Z',
        });
        expect(result.hours).toBeCloseTo(2, 0);
    });
});


describe('getAssetStaleness — path 2: fallbackLastUpdated', () => {
    it('uses fallback when recentEvents has no match for this asset', () => {
        const result = getAssetStaleness('orders', [], {
            fallbackLastUpdated: '2026-05-07T22:30:00Z',  // ~13.5h ago
        });
        // Result is non-fresh — the exact label depends on thresholds, but
        // the key point is that it's NOT unknown / "No data".
        expect(result.status).not.toBe('unknown');
        expect(result.label).not.toBe('No data');
        expect(result.hours).toBeGreaterThan(0);
    });

    it('uses fallback when recentEvents has events for OTHER assets only', () => {
        // Realistic scenario: date picker shows today's events for some
        // assets but not the one we're rendering.
        const events = [
            { asset_name: 'inventory', event_time: '2026-05-08T11:00:00Z' },
        ];
        const result = getAssetStaleness('orders', events, {
            fallbackLastUpdated: '2026-05-06T08:00:00Z',
        });
        expect(result.status).not.toBe('unknown');
    });

    it('uses fallback when recentEvents is null', () => {
        const result = getAssetStaleness('orders', null, {
            fallbackLastUpdated: '2026-05-08T10:00:00Z',
        });
        expect(result.status).toBe('fresh');
    });
});


describe('getAssetStaleness — path 3: truly no data', () => {
    it('returns unknown when neither recentEvents nor fallback have data', () => {
        const result = getAssetStaleness('orders', []);
        expect(result.status).toBe('unknown');
        expect(result.label).toBe('No data');
        expect(result.hours).toBeNull();
    });

    it('returns unknown for empty fallback string', () => {
        // Backend returns '' when no events exist for an asset — must not
        // be treated as a valid timestamp.
        const result = getAssetStaleness('never_run', [], {
            fallbackLastUpdated: '',
        });
        expect(result.status).toBe('unknown');
    });

    it('returns unknown when recentEvents entry has no event_time', () => {
        // Defensive: malformed event payload doesn't crash, falls through.
        const events = [
            { asset_name: 'orders' /* event_time missing */ },
        ];
        const result = getAssetStaleness('orders', events);
        expect(result.status).toBe('unknown');
    });
});
