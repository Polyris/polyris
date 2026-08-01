// Format duration in human readable format
export const formatDuration = (ms: number | null | undefined): string => {
    if (!ms && ms !== 0) return '-';
    const s = Math.floor(ms / 1000);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
};

// Format time as HH:MM:SS
export const formatTime = (iso: string | null | undefined): string => {
    if (!iso) return '-';
    try {
        const d = new Date(iso);
        return d.toLocaleTimeString('en-US', { hour12: false });
    } catch {
        return '-';
    }
};


/**
 * Format a timestamp as a date-aware string. Use this anywhere a user
 * needs to know *when* something happened, including across days. Unlike
 * `formatTime` (HH:MM:SS only), this disambiguates:
 *
 *   - Same calendar day  → "Today at 21:29:43"
 *   - Yesterday          → "Yesterday at 21:29:43"
 *   - This year          → "May 7 at 21:29:43"
 *   - Other years        → "May 7, 2025 at 21:29:43"
 *
 * Banner and "Latest Execution" cards on the Asset Detail page used
 * `formatTime` and showed e.g. "21:29:43" with no date — visually
 * indistinguishable from "this happened a minute ago" when the event
 * was actually yesterday. This helper makes the date explicit without
 * the verbosity of full ISO format.
 */
export const formatDateTime = (iso: string | null | undefined): string => {
    if (!iso) return '-';
    try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '-';

        const now = new Date();
        const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const yesterday = new Date(today);
        yesterday.setDate(today.getDate() - 1);
        const eventDay = new Date(d.getFullYear(), d.getMonth(), d.getDate());

        const time = d.toLocaleTimeString('en-US', { hour12: false });

        if (eventDay.getTime() === today.getTime()) {
            return `Today at ${time}`;
        }
        if (eventDay.getTime() === yesterday.getTime()) {
            return `Yesterday at ${time}`;
        }
        const sameYear = d.getFullYear() === now.getFullYear();
        const datePart = d.toLocaleDateString('en-US', sameYear
            ? { month: 'short', day: 'numeric' }
            : { month: 'short', day: 'numeric', year: 'numeric' }
        );
        return `${datePart} at ${time}`;
    } catch {
        return '-';
    }
};

// Format countdown from wait_until timestamp or seconds
export const formatCountdown = (input: number | string | null | undefined): string | null => {
    if (!input && input !== 0) return null;
    
    let seconds: number;
    
    // If input is a number, treat as seconds
    if (typeof input === 'number') {
        seconds = input;
    } else {
        // If input is a string/timestamp, calculate remaining time
        try {
            const target = new Date(input).getTime();
            const now = Date.now();
            const diff = target - now;
            if (diff <= 0) return '0s';
            seconds = Math.floor(diff / 1000);
        } catch {
            return null;
        }
    }
    
    if (seconds <= 0) return '0s';
    if (seconds < 60) return `${seconds}s`;
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m ${seconds % 60}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
};

// Convert date to YYYY-MM-DD string
export const toDateString = (date: Date): string => {
    if (!date) return '';
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
};

// Format ISO date for display
export const formatDate = (iso: string | null | undefined): string => {
    if (!iso) return '-';
    try {
        const d = new Date(iso);
        return d.toLocaleString('en-US', { 
            month: 'short', 
            day: 'numeric', 
            hour: '2-digit', 
            minute: '2-digit',
            hour12: false 
        });
    } catch {
        return '-';
    }
};

// Build AWS Console URL for Step Functions execution
export const buildAwsConsoleUrl = (arn: string | null | undefined): string => {
    if (!arn) return '#';
    const parts = arn.split(':');
    if (parts.length < 7) return '#';
    const region = parts[3];
    return `https://${region}.console.aws.amazon.com/states/home?region=${region}#/v2/executions/details/${encodeURIComponent(arn)}`;
};

/** Extract message from unknown catch value (avoids `as Error` assertions) */
export const getErrorMessage = (err: unknown): string =>
    err instanceof Error ? err.message : String(err);

/** Extract readable message from API response error (shows detail if present) */
export const getApiErrorMessage = (result: { error?: string; detail?: string }): string => {
    if (!result.error) return 'Unknown error';
    return result.detail ? `${result.error}: ${result.detail}` : result.error;
};

/** Type-safe string array extraction (for API fields like tags) */
export const asStringArray = (value: unknown): string[] =>
    Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];

/** Type-safe Record extraction */
export const asRecord = <V = unknown>(value: unknown): Record<string, V> =>
    (value && typeof value === 'object' && !Array.isArray(value)) ? value as Record<string, V> : {};

/** Type-safe click-outside handler */
export const onClickOutside = (
    ref: React.RefObject<HTMLElement | null>,
    handler: () => void
): { add: () => void; remove: () => void } => {
    const listener = (e: MouseEvent) => {
        if (ref.current && !ref.current.contains(e.target as Node)) handler();
    };
    return {
        add: () => document.addEventListener('mousedown', listener),
        remove: () => document.removeEventListener('mousedown', listener),
    };
};

/**
 * formatUser — normalize a "started_by" / "owner" field for UI display.
 *
 * Backends sometimes return the literal string "unknown" instead of null
 * when auth context is missing (e.g. a Step Function-initiated backfill
 * has no human caller). Showing the word "unknown" in the UI suggests
 * something failed; an em-dash is the correct UX signal for "no value".
 *
 * Empty strings and whitespace-only also collapse to '—'.
 *
 * @example
 * formatUser(null)         // '—'
 * formatUser('unknown')    // '—'
 * formatUser('')           // '—'
 * formatUser('  ')         // '—'
 * formatUser('mike@x.com') // 'mike@x.com'
 */
export const formatUser = (value: string | null | undefined): string => {
    if (!value) return '—';
    const trimmed = value.trim();
    if (!trimmed) return '—';
    if (trimmed.toLowerCase() === 'unknown') return '—';
    return trimmed;
};

/**
 * formatRelativeTime — convert an ISO timestamp to a compact relative
 * string suitable for dense tables. The full timestamp is meant to live
 * in the surrounding element's title/hover.
 *
 * Tiers:
 *   - < 60s    →  "just now"
 *   - < 60min  →  "Nm ago"
 *   - < 24h    →  "Nh ago"
 *   - < 7d     →  "Nd ago"
 *   - ≥ 7d     →  ISO date (e.g. "2026-04-15")
 *
 * Future timestamps (clock skew) display as "just now".
 *
 * @example
 * formatRelativeTime(null)                       // '—'
 * formatRelativeTime(new Date().toISOString())   // 'just now'
 */
export const formatRelativeTime = (iso: string | null | undefined): string => {
    if (!iso) return '—';
    let then: number;
    try {
        then = new Date(iso).getTime();
        if (Number.isNaN(then)) return '—';
    } catch {
        return '—';
    }
    const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
    if (diffSec < 60) return 'just now';
    const diffMin = Math.floor(diffSec / 60);
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24) return `${diffHr}h ago`;
    const diffDay = Math.floor(diffHr / 24);
    if (diffDay < 7) return `${diffDay}d ago`;
    // ≥ 7d: fall back to ISO date (YYYY-MM-DD) for unambiguous reference.
    return iso.slice(0, 10);
};

// Human-readable schedule label for a pipeline. Single source of truth for
// schedule display — used by the sidebar, command palette, and anywhere else
// a schedule is shown.
//
// assetSchedule (optional): an asset-triggered pipeline has an empty
// `schedule` string (it has no cron/rate — see ADR on asset_subscriptions_table
// registration) but a populated asset_schedule instead. Without checking it,
// such a pipeline is indistinguishable from a genuinely manual one — both
// hit the `!schedule` branch below and show "Manual", which is wrong: an
// asset-triggered pipeline runs automatically, just not on a timer.
export function formatSchedule(
    schedule: string | undefined | null,
    assetSchedule?: { operator: 'AND' | 'OR'; assets: string[] } | null,
): string {
    if (!schedule) {
        if (assetSchedule && assetSchedule.assets.length > 0) {
            const joiner = assetSchedule.operator === 'OR' ? ' | ' : ' & ';
            const label = assetSchedule.assets.join(joiner);
            return label.length > 24 ? label.substring(0, 24) + '…' : label;
        }
        return 'Manual';
    }

    // Rate expressions: rate(1 hour), rate(6 hours)
    const rateMatch = schedule.match(/rate\((\d+)\s*(minute|hour|day)s?\)/i);
    if (rateMatch) {
        const [, val, unit] = rateMatch;
        const abbr: Record<string, string> = { minute: 'min', hour: 'h', day: 'd' };
        return `every ${val}${abbr[unit.toLowerCase()] || unit}`;
    }

    // Cron: cron(min hour dom month dow year)
    const cronMatch = schedule.match(/cron\((\d+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\)/);
    if (cronMatch) {
        const [, min, hour, , , dow] = cronMatch;
        const time = `${hour.padStart(2, '0')}:${min.padStart(2, '0')}`;
        const dayNames: Record<string, string> = { MON: 'Mon', TUE: 'Tue', WED: 'Wed', THU: 'Thu', FRI: 'Fri', SAT: 'Sat', SUN: 'Sun' };
        if (dow === '*' || dow === '?') return `daily @ ${time}`;
        if (dayNames[dow]) return `${dayNames[dow]} @ ${time}`;
        return `@ ${time}`;
    }

    // Fallback: show raw but truncated
    return schedule.length > 20 ? schedule.substring(0, 20) + '…' : schedule;
}

// Human-readable freshness window for an asset dependency (wait_for's
// freshness_hours). The raw value can be a long repeating decimal —
// Asset.within(minutes=2) produces 0.0333333333333333 — so this picks
// whichever unit (days/hours/minutes) renders cleanly, rounding to at most
// one decimal place rather than showing the raw float.
export function formatFreshnessWindow(hours: number): string {
    if (hours >= 24 && hours % 24 === 0) return `${hours / 24}d`;
    if (hours >= 1) {
        const rounded = Math.round(hours * 10) / 10;
        return `${rounded}h`;
    }
    return `${Math.round(hours * 60)}m`;
}

// Same conversion, as a full sentence fragment for tooltips
// ("Must be fresh within {formatFreshnessWindowLong(hours)}").
export function formatFreshnessWindowLong(hours: number): string {
    if (hours >= 24 && hours % 24 === 0) {
        const days = hours / 24;
        return `${days} day${days !== 1 ? 's' : ''}`;
    }
    if (hours >= 1) {
        const rounded = Math.round(hours * 10) / 10;
        return `${rounded} hour${rounded !== 1 ? 's' : ''}`;
    }
    const minutes = Math.round(hours * 60);
    return `${minutes} minute${minutes !== 1 ? 's' : ''}`;
}
