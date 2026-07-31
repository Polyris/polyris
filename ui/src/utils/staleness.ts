/**
 * Staleness Calculation Utility
 * 
 * Single source of truth for asset staleness calculations.
 * Used by: AssetLineageFlow.jsx, AssetsView.jsx
 */

import { STALENESS, MS } from './constants';
import type { StalenessResult } from '../types';

// Re-export for backward compatibility
export const STALE_THRESHOLD_HOURS = STALENESS.STALE_HOURS;
export const WARNING_THRESHOLD_HOURS = STALENESS.WARNING_HOURS;

/**
 * Calculate staleness status based on event time.
 */
export function calculateStaleness(eventTime: Date | string | null, options: { staleHours?: number; warningHours?: number } = {}): StalenessResult {
    const { 
        staleHours = STALENESS.STALE_HOURS, 
        warningHours = STALENESS.WARNING_HOURS 
    } = options;

    if (!eventTime) {
        return { status: 'unknown', label: 'No data', hours: null };
    }

    const eventDate = eventTime instanceof Date ? eventTime : new Date(eventTime);
    
    // Handle invalid dates
    if (isNaN(eventDate.getTime())) {
        return { status: 'unknown', label: 'Invalid date', hours: null };
    }

    const now = new Date();
    const diffHours = (now.getTime() - eventDate.getTime()) / MS.HOUR;

    if (diffHours > staleHours) {
        const days = Math.floor(diffHours / 24);
        return { 
            status: 'stale', 
            label: `Stale (${days}d ago)`,
            hours: diffHours 
        };
    } else if (diffHours > warningHours) {
        return { 
            status: 'warning', 
            label: `${Math.floor(diffHours)}h ago`,
            hours: diffHours 
        };
    } else {
        return { 
            status: 'fresh', 
            label: `${Math.floor(diffHours)}h ago`,
            hours: diffHours 
        };
    }
}

/**
 * Get staleness for an asset.
 *
 * Resolution order (newest known time wins):
 *   1. If `recentEvents` contains an event for this asset → use that
 *      event_time. recentEvents is scoped to the date picker, so this
 *      is the right path when the picker is on a day when the asset
 *      did materialize.
 *   2. Else if `fallbackLastUpdated` is set → use that. The lineage
 *      endpoint stamps each asset with its most-recent event_time
 *      independently of the date picker (see backend
 *      `_enrich_assets_with_last_updated`), so we can show "2h ago"
 *      for an asset that materialized yesterday even when the picker
 *      is on today.
 *   3. Else → unknown / "No data" — no events found at all.
 *
 * Step 2 was added in v0.75.5 to fix a v0.75.3 regression: previously
 * any asset whose latest event fell outside the picker's day showed
 * "Never", which was misleading because the events existed, just on
 * other dates. Now the fallback surfaces the real last-known time.
 *
 * @param assetName - Asset name to look up
 * @param recentEvents - Date-scoped events from /recent-events
 * @param options - Optional staleness thresholds + fallback timestamp
 */
export function getAssetStaleness(
    assetName: string,
    recentEvents: Array<{ asset_name: string; event_time?: string }> | null,
    options: {
        staleHours?: number;
        warningHours?: number;
        /** Asset's `last_updated` from the lineage endpoint —
         *  used when recentEvents has no entry for this asset. */
        fallbackLastUpdated?: string;
    } = {},
): StalenessResult {
    // Path 1: scoped recentEvents
    if (Array.isArray(recentEvents)) {
        const recent = recentEvents.find(e => e.asset_name === assetName);
        if (recent && recent.event_time) {
            return calculateStaleness(recent.event_time, options);
        }
    }

    // Path 2: backend-provided fallback (cross-date, independent of picker)
    if (options.fallbackLastUpdated) {
        return calculateStaleness(options.fallbackLastUpdated, options);
    }

    // Path 3: nothing known
    return { status: 'unknown', label: 'No data', hours: null };
}
