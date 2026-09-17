/**
 * Detects the synthetic marker polyris writes when an operator resolves a task
 * via the UI (mark_success / skip / fail / stop). Both UpstreamDep (when a
 * downstream task shows an upstream that was resolved manually) and OutputCard
 * (when the manually-resolved task's own Output tab is opened) route through
 * this so the render rule lives in one place.
 *
 * The marker shape is set by `console_api/routes/tasks.py::_write_synthetic_output_marker`:
 *   { _manually_resolved: true, _resolution: "mark_success"|"skip"|"fail"|"stop",
 *     _reason: string, _operator: string }
 *
 * Returns null if the value isn't a manual-resolution marker — callers fall
 * back to the organic-output rendering path.
 */
import type { ManualResolution as ManualResolutionValue } from '@/generated/enums';

// Re-export so consumers in this folder can import the generated union from
// one place. The generated file is the single source of truth (regenerated
// by `make generate-enums` from polyris/constants.py::ManualResolution).
export type { ManualResolution as ManualResolutionValue } from '@/generated/enums';

export interface ManualResolution {
    // Widened to string because the marker's `_resolution` field is arbitrary
    // JSON — an unknown value should still render as "Resolved (X) by …"
    // rather than crash the detector; the generated union documents what the
    // backend actually writes.
    resolution: ManualResolutionValue | string;
    reason: string;
    operator: string;
}

export function detectManualResolution(value: unknown): ManualResolution | null {
    if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
    const rec = value as Record<string, unknown>;
    if (rec._manually_resolved !== true) return null;
    return {
        resolution: typeof rec._resolution === 'string' ? rec._resolution : 'unknown',
        reason: typeof rec._reason === 'string' ? rec._reason : '',
        // Backend added `_operator` in 0.100.0 — records written before that
        // deploy carry no operator field. Fall back to a generic label so old
        // rows still render without a UI-side branch.
        operator: typeof rec._operator === 'string' && rec._operator ? rec._operator : 'operator',
    };
}

/**
 * Format a one-line human-readable summary for the card preview and header.
 * "Marked success by alice@… — verified via S3 logs"
 */
export function formatManualResolution(m: ManualResolution): string {
    const verb = m.resolution === 'mark_success' ? 'Marked success'
        : m.resolution === 'skip' ? 'Skipped'
        : m.resolution === 'fail' ? 'Marked failed'
        : m.resolution === 'stop' ? 'Stopped'
        : `Resolved (${m.resolution})`;
    const by = ` by ${m.operator}`;
    const reason = m.reason ? ` — ${m.reason}` : '';
    return `${verb}${by}${reason}`;
}

/**
 * Map a manual resolution to the DDB task status the backend writes for it.
 * (See ``console_api/routes/tasks.py::_execute_task_action`` — the
 * ``target_status`` per action.) Used by OutputCard so its primary badge
 * mirrors what UpstreamDep would render for the same task instead of a
 * hardcoded green ``success`` — the docs' "applied identically" claim.
 */
export function statusForResolution(resolution: string): string {
    switch (resolution) {
        case 'mark_success': return 'success';
        case 'skip': return 'skipped';
        case 'fail': return 'failed';
        case 'stop': return 'stopped';
        default: return resolution;
    }
}

/**
 * Pick the semantic UI variant for a resolved-status badge. Manual skip /
 * fail / stop are still red-ish (the task did not produce data) even though
 * the intervention was intentional; mark_success stays green.
 */
export function variantForResolution(resolution: string): 'success' | 'error' | 'muted' {
    if (resolution === 'mark_success') return 'success';
    if (resolution === 'stop') return 'muted';
    return 'error'; // skip / fail / anything else
}
