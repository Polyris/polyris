import React from 'react';
import { X } from '../utils/icons';

export interface WorkspaceMetric {
    /** Short label under the value, e.g. 'Running'. */
    label: string;
    /** The metric value — a count or short string. */
    value: number | string;
    /** Colour tone for the value. */
    tone?: 'neutral' | 'success' | 'running' | 'failed' | 'warning';
}

/**
 * A compact row of at-a-glance metrics for a workspace header. Used by the Team overlay
 * (Assets, Backfills list); the free History view dropped its metric cards. Purely
 * presentational: the caller derives the numbers and tones. Styled via `.ws-metrics` /
 * `.ws-metric` design tokens, so it themes light/dark automatically. Renders nothing when
 * there are no metrics.
 */
export function WorkspaceMetrics({ metrics }: { metrics: WorkspaceMetric[] }) {
    if (!metrics.length) return null;
    return (
        <div className="ws-metrics" role="group" aria-label="Summary metrics">
            {metrics.map((m, i) => (
                <div key={i} className={`ws-metric ws-metric--${m.tone || 'neutral'}`}>
                    <span className="ws-metric-value">{m.value}</span>
                    <span className="ws-metric-label">{m.label}</span>
                </div>
            ))}
        </div>
    );
}

export default WorkspaceMetrics;

export interface WorkspaceFilterChip {
    /** What the filter is on, e.g. 'Status'. */
    label: string;
    /** The current filter value. */
    value: string;
    /** Remove just this filter. */
    onRemove: () => void;
}

/**
 * A row of removable chips for the active filters on a workspace list. Each chip
 * clears just its own filter; the caller still owns a "Clear all" control. Renders
 * nothing when no filters are active. Styled via `.ws-filter-chip` design tokens.
 */
export function WorkspaceFilterChips({ chips }: { chips: WorkspaceFilterChip[] }) {
    if (!chips.length) return null;
    return (
        <div className="ws-filter-chips" role="group" aria-label="Active filters">
            {chips.map((c, i) => (
                <button
                    key={i}
                    type="button"
                    className="ws-filter-chip"
                    onClick={c.onRemove}
                    title={`Remove ${c.label} filter`}
                >
                    <span className="ws-filter-chip-label">{c.label}:</span>
                    <span className="ws-filter-chip-value">{c.value}</span>
                    <X size={12} />
                </button>
            ))}
        </div>
    );
}
