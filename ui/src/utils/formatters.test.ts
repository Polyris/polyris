/**
 * Tests for `formatDateTime` (v0.75.6).
 *
 * Banner and Latest Execution cards previously showed `formatTime` output
 * ("21:29:43") with no date context — visually indistinguishable from
 * "this happened a moment ago" when the event was actually yesterday.
 * `formatDateTime` makes the date explicit while staying readable.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { formatDateTime } from './formatters';

// "Now" is fixed so "Today"/"Yesterday" branches are deterministic.
const NOW = new Date('2026-05-08T14:00:00Z');

beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
});

afterEach(() => {
    vi.useRealTimers();
});


describe('formatDateTime', () => {
    it('returns "-" for null', () => {
        expect(formatDateTime(null)).toBe('-');
    });

    it('returns "-" for undefined', () => {
        expect(formatDateTime(undefined)).toBe('-');
    });

    it('returns "-" for empty string', () => {
        expect(formatDateTime('')).toBe('-');
    });

    it('returns "-" for invalid date strings', () => {
        expect(formatDateTime('not a date')).toBe('-');
    });

    it('shows "Today at HH:MM:SS" for events on the current day', () => {
        // "Now" is set to UTC; jsdom uses the host TZ for `toLocaleTimeString`,
        // so we assert the prefix and the structure rather than exact time
        // (the host TZ in CI may be UTC, in dev maybe local — both pass).
        const result = formatDateTime('2026-05-08T13:00:00Z');
        expect(result).toMatch(/^Today at \d{2}:\d{2}:\d{2}$/);
    });

    it('shows "Yesterday at HH:MM:SS" for events on the previous day', () => {
        const result = formatDateTime('2026-05-07T21:29:43Z');
        expect(result).toMatch(/^Yesterday at \d{2}:\d{2}:\d{2}$/);
    });

    it('shows "Mon D at HH:MM:SS" for events earlier this year', () => {
        const result = formatDateTime('2026-03-15T10:00:00Z');
        // "Mar 15 at 10:00:00" — month name + day, no year because same year as NOW.
        expect(result).toMatch(/^Mar 15 at \d{2}:\d{2}:\d{2}$/);
    });

    it('shows "Mon D, YYYY at HH:MM:SS" for events in different years', () => {
        const result = formatDateTime('2024-12-01T08:30:00Z');
        // "Dec 1, 2024 at 08:30:00" — full date including year.
        expect(result).toMatch(/^Dec 1, 2024 at \d{2}:\d{2}:\d{2}$/);
    });

    it('disambiguates yesterday from earlier in the same week', () => {
        // 5 days ago — should NOT be "Yesterday".
        const fiveDaysAgo = formatDateTime('2026-05-03T12:00:00Z');
        expect(fiveDaysAgo).not.toMatch(/Yesterday/);
        expect(fiveDaysAgo).not.toMatch(/Today/);
        expect(fiveDaysAgo).toMatch(/^May 3 at /);
    });
});

// ────────────────────────────────────────────────────────────────────────────
// formatUser (v0.78.2) — "unknown" / null / empty all collapse to em-dash.
// Real usernames pass through.
// ────────────────────────────────────────────────────────────────────────────

import { formatUser, formatRelativeTime } from './formatters';

describe('formatUser', () => {
    it('returns em-dash for null/undefined', () => {
        expect(formatUser(null)).toBe('—');
        expect(formatUser(undefined)).toBe('—');
    });

    it('returns em-dash for empty / whitespace strings', () => {
        expect(formatUser('')).toBe('—');
        expect(formatUser('   ')).toBe('—');
    });

    it('returns em-dash for literal "unknown" (any case)', () => {
        expect(formatUser('unknown')).toBe('—');
        expect(formatUser('UNKNOWN')).toBe('—');
        expect(formatUser('Unknown')).toBe('—');
    });

    it('passes real usernames through, trimmed', () => {
        expect(formatUser('mike@x.com')).toBe('mike@x.com');
        expect(formatUser('  mike@x.com  ')).toBe('mike@x.com');
    });
});

// ────────────────────────────────────────────────────────────────────────────
// formatRelativeTime (v0.78.2) — compact "Nm ago" / "Nh ago" / ISO date.
// Note: tests use a frozen Date.now() via vi.useFakeTimers() to be stable.
// ────────────────────────────────────────────────────────────────────────────

describe('formatRelativeTime', () => {
    beforeEach(() => {
        vi.useFakeTimers();
        // Fixed reference: 2026-05-27 12:00:00 UTC
        vi.setSystemTime(new Date('2026-05-27T12:00:00Z'));
    });
    afterEach(() => {
        vi.useRealTimers();
    });

    it('returns em-dash for null/undefined', () => {
        expect(formatRelativeTime(null)).toBe('—');
        expect(formatRelativeTime(undefined)).toBe('—');
    });

    it('returns em-dash for unparseable input', () => {
        expect(formatRelativeTime('not-a-date')).toBe('—');
    });

    it('< 60s ago → "just now"', () => {
        expect(formatRelativeTime('2026-05-27T11:59:30Z')).toBe('just now');
    });

    it('< 60min ago → "Nm ago"', () => {
        expect(formatRelativeTime('2026-05-27T11:55:00Z')).toBe('5m ago');
        expect(formatRelativeTime('2026-05-27T11:01:00Z')).toBe('59m ago');
    });

    it('< 24h ago → "Nh ago"', () => {
        expect(formatRelativeTime('2026-05-27T09:00:00Z')).toBe('3h ago');
    });

    it('< 7d ago → "Nd ago"', () => {
        expect(formatRelativeTime('2026-05-25T12:00:00Z')).toBe('2d ago');
    });

    it('≥ 7d ago → ISO date', () => {
        expect(formatRelativeTime('2026-04-15T12:00:00Z')).toBe('2026-04-15');
    });

    it('future timestamps clamp to "just now"', () => {
        expect(formatRelativeTime('2026-05-28T12:00:00Z')).toBe('just now');
    });
});

import { formatSchedule, formatFreshnessWindow, formatFreshnessWindowLong } from './formatters';

describe('formatSchedule', () => {
    it('returns "Manual" for empty / missing schedule (on-demand pipelines)', () => {
        expect(formatSchedule('')).toBe('Manual');
        expect(formatSchedule(undefined)).toBe('Manual');
        expect(formatSchedule(null)).toBe('Manual');
    });

    it('formats rate expressions', () => {
        expect(formatSchedule('rate(6 hours)')).toBe('every 6h');
        expect(formatSchedule('rate(1 minute)')).toBe('every 1min');
    });

    it('formats cron expressions', () => {
        expect(formatSchedule('cron(0 9 * * ? *)')).toBe('daily @ 09:00');
        expect(formatSchedule('cron(30 14 ? * MON *)')).toBe('Mon @ 14:30');
    });

    it('truncates unrecognized long schedules', () => {
        expect(formatSchedule('some-very-long-custom-expression-here')).toBe('some-very-long-custo…');
    });

    it('still returns "Manual" for a genuinely manual pipeline (no asset_schedule either)', () => {
        expect(formatSchedule('', null)).toBe('Manual');
        expect(formatSchedule('', undefined)).toBe('Manual');
        expect(formatSchedule(undefined, null)).toBe('Manual');
    });

    it('shows the trigger asset for an asset-triggered pipeline instead of "Manual"', () => {
        // This is the exact shape reported as a bug: an asset-triggered
        // pipeline's `schedule` field is empty, which previously fell
        // through to "Manual" — indistinguishable from a truly manual one.
        expect(formatSchedule('', { operator: 'AND', assets: ['clean/orders'] }))
            .toBe('clean/orders');
    });

    it('joins multiple assets with "&" for AND and "|" for OR', () => {
        expect(formatSchedule('', { operator: 'AND', assets: ['sales', 'inventory'] }))
            .toBe('sales & inventory');
        expect(formatSchedule('', { operator: 'OR', assets: ['sales', 'inventory'] }))
            .toBe('sales | inventory');
    });

    it('truncates a long asset-schedule label the same way as a raw schedule', () => {
        expect(formatSchedule('', { operator: 'AND', assets: ['some/very/long/asset/name/here'] }))
            .toBe('some/very/long/asset/nam…');
    });

    it('ignores asset_schedule entirely when schedule is already a real cron/rate', () => {
        // A time-based pipeline should never show an asset label even if
        // asset_schedule were somehow present — schedule takes priority.
        expect(formatSchedule('rate(6 hours)', { operator: 'AND', assets: ['clean/orders'] }))
            .toBe('every 6h');
    });
});

describe('formatFreshnessWindow', () => {
    it('formats the exact repeating decimal that prompted this — 2 minutes', () => {
        // Asset.within(minutes=2) produces freshness_hours = 0.0333333333333333 —
        // this must not be shown raw.
        expect(formatFreshnessWindow(2 / 60)).toBe('2m');
    });

    it('formats whole hours without a decimal', () => {
        expect(formatFreshnessWindow(12)).toBe('12h');
        expect(formatFreshnessWindow(1)).toBe('1h');
    });

    it('formats a fractional-hour value that is not exactly on a minute boundary', () => {
        expect(formatFreshnessWindow(1.5)).toBe('1.5h');
    });

    it('formats whole multiples of 24h as days, including exactly 24', () => {
        expect(formatFreshnessWindow(48)).toBe('2d');
        expect(formatFreshnessWindow(24)).toBe('1d');
    });

    it('formats sub-hour values as minutes', () => {
        expect(formatFreshnessWindow(0.5)).toBe('30m');
    });
});

describe('formatFreshnessWindowLong', () => {
    it('pluralizes correctly at each boundary', () => {
        expect(formatFreshnessWindowLong(2 / 60)).toBe('2 minutes');
        expect(formatFreshnessWindowLong(1 / 60)).toBe('1 minute');
        expect(formatFreshnessWindowLong(1)).toBe('1 hour');
        expect(formatFreshnessWindowLong(12)).toBe('12 hours');
        expect(formatFreshnessWindowLong(24)).toBe('1 day');
        expect(formatFreshnessWindowLong(48)).toBe('2 days');
    });
});
