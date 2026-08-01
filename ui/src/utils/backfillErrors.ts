/**
 * Backfill error code → user-friendly message with actionable recovery hint.
 *
 * Backend (sam/lambdas/console_api/routes/backfill.py) returns errors as
 * `{ error: 'code', message: 'detail' }`. The set of codes is the canonical
 * registry BACKFILL_ERROR_CODES, generated from polyris/constants.py into
 * @/generated/enums. This map MUST have an entry for every code in that
 * registry — enforced by backfillErrors.test.ts (keys ⊇ BACKFILL_ERROR_CODES),
 * which replaces the old hand-maintained "critical codes" list that silently
 * fell behind the backend (ADR #94). If a code is somehow missing here, the UI
 * falls back to the backend's raw `message`.
 */

export interface BackfillErrorInfo {
    /** Short user-facing title (toast headline). */
    title: string;
    /** Concrete action the user can take to recover. */
    hint?: string;
}

const BACKFILL_ERROR_MAP: Record<string, BackfillErrorInfo> = {
    // ── Validation: bad input shape ────────────────────────────────────────
    invalid_target: {
        title: 'Invalid backfill target',
        hint: 'Specify a pipeline name or asset name.',
    },
    invalid_target_type: {
        title: 'Target type not supported',
        hint: 'Use type "pipeline" or "asset". "batch" is reserved for v0.79+.',
    },
    target_not_found: {
        title: 'Target not found',
        hint: 'Verify the pipeline/asset name exists and is deployed.',
    },
    no_producer: {
        title: 'No producer pipeline for asset',
        hint: 'This asset is not produced by any pipeline in the registry.',
    },
    multi_producer_asset: {
        title: 'Asset has multiple producers',
        hint: 'Target the specific producer pipeline directly instead.',
    },
    producer_pipeline_missing: {
        title: 'Producer pipeline not in registry',
        hint: 'The asset names a producer pipeline that isn\'t deployed. Deploy it or fix the asset definition.',
    },
    unreachable_target_type: {
        title: 'Unsupported target type',
        hint: 'Only "pipeline" and "asset" targets are supported.',
    },

    // ── Validation: partitions ─────────────────────────────────────────────
    invalid_partitions: {
        title: 'Invalid partition specification',
        hint: 'Use either {start, end} for a range or {keys: [...]} for explicit partitions.',
    },
    invalid_partition_format: {
        title: 'Partition format doesn\'t match granularity',
        hint: 'For daily use YYYY-MM-DD; weekly YYYY-Www; monthly YYYY-MM; hourly YYYY-MM-DDTHH.',
    },
    range_too_large: {
        title: 'Backfill range exceeds 1000 partitions',
        hint: 'Split into smaller windows (e.g., 1000-day chunks) and submit separately.',
    },
    invalid_partition_keys: {
        title: 'Invalid partition keys',
        hint: 'Provide a non-empty list of partition keys matching the target granularity.',
    },
    partition_keys_not_failed: {
        title: 'Some partitions aren\'t in a failed state',
        hint: 'Retry-failed only re-runs failed partitions. Remove the non-failed keys from the request.',
    },

    // ── Validation: options ────────────────────────────────────────────────
    invalid_options: {
        title: 'Invalid options',
        hint: 'Check max_parallel is between 1 and 10.',
    },
    invalid_granularity_override: {
        title: 'Invalid granularity override',
        hint: 'Override must be one of: hourly, daily, weekly, monthly.',
    },
    granularity_override_not_allowed: {
        title: 'Granularity override not allowed',
        hint: 'Override is only accepted when the pipeline\'s cron is ambiguous.',
    },
    // ── Validation: downstream / upstream lineage (ADR #91 / #92) ──────────
    invalid_downstream: {
        title: 'Invalid downstream option',
        hint: 'Downstream must be one of: auto, all, none.',
    },
    invalid_downstream_for_pipeline_target: {
        title: 'Downstream option not allowed for pipeline target',
        hint: 'Downstream applies to asset targets only. Remove it or target an asset.',
    },
    invalid_upstream: {
        title: 'Invalid upstream option',
        hint: 'Upstream must be one of: off, smart, force.',
    },
    invalid_upstream_for_pipeline_target: {
        title: 'Upstream option not allowed for pipeline target',
        hint: 'Upstream smart-fill applies to asset targets only. Remove it or target an asset.',
    },
    upstream_cycle: {
        title: 'Dependency cycle in upstream lineage',
        hint: 'The asset graph has a cycle, so an upstream build order can\'t be computed. Fix the pipeline dependencies.',
    },
    invalid_tasks: {
        title: 'Invalid tasks list',
        hint: 'tasks must be a list of task names belonging to the target pipeline.',
    },

    // ── Concurrency / state ────────────────────────────────────────────────
    concurrent_backfill_active: {
        title: 'Another backfill is already running',
        hint: 'Wait for it to finish, cancel it first, or set options.allow_concurrent=true to override (risk of duplicate work).',
    },
    nothing_to_run: {
        title: 'Nothing to run',
        hint: 'All requested partitions are already complete. Disable skip_completed to force re-run.',
    },
    not_found: {
        title: 'Backfill not found',
        hint: 'It may have been deleted or is older than its retention window.',
    },
    not_eligible: {
        title: 'Backfill not eligible for this action',
        hint: 'The backfill\'s current state doesn\'t allow this operation.',
    },
    child_name_too_long: {
        title: 'Generated execution name too long',
        hint: 'The pipeline/partition combination exceeds the Step Functions name limit. Use a shorter pipeline name or narrower range.',
    },
    already_terminal: {
        title: 'Backfill already finished',
        hint: 'Can\'t cancel a backfill that\'s already completed, failed, or canceled.',
    },
    nothing_to_retry: {
        title: 'No failed partitions to retry',
        hint: 'This backfill has no partitions in failed state.',
    },
    malformed_parent: {
        title: 'Parent backfill record is corrupted',
        hint: 'Contact support — this backfill\'s record is unreadable.',
    },

    // ── Infrastructure ─────────────────────────────────────────────────────
    throttled: {
        title: 'AWS throttled the request',
        hint: 'Wait a few seconds and try again.',
    },
    sfn_start_failed: {
        title: 'Step Functions failed to start',
        hint: 'Check CloudWatch logs for the bulk-backfill state machine.',
    },
    misconfigured: {
        title: 'Backfill infrastructure misconfigured',
        hint: 'BULK_BACKFILL_ARN missing — contact your platform admin.',
    },
    id_space_exhausted: {
        title: 'Backfill ID collision',
        hint: 'Retry — extremely rare collision in ID generation.',
    },
    internal_error: {
        title: 'Something went wrong',
        hint: 'An unexpected error occurred. Retry; if it persists, check CloudWatch logs.',
    },
    status_race: {
        title: 'Backfill changed state concurrently',
        hint: 'Another action updated this backfill at the same time. Refresh and try again.',
    },
    malformed_body: {
        title: 'Malformed request',
        hint: 'The request body couldn\'t be parsed. This is usually a client bug — retry from the UI.',
    },
};

/**
 * Format a backfill error for UI display.
 *
 * @param err Error object (from fetch / mutation throw) OR a backend
 *   response body with { error, message }.
 * @returns Object with `title` for toast headline + optional `hint`.
 */
export function formatBackfillError(err: unknown): BackfillErrorInfo {
    // Extract error code + message from various shapes
    let code: string | undefined;
    let backendMessage: string | undefined;

    if (err instanceof Error) {
        // Errors from useMutation wrap fetch failures; the body may be
        // serialized in the message
        try {
            const parsed = JSON.parse(err.message);
            code = parsed.error;
            backendMessage = parsed.message;
        } catch {
            backendMessage = err.message;
        }
    } else if (err && typeof err === 'object') {
        code = (err as { error?: string }).error;
        backendMessage = (err as { message?: string }).message;
    }

    if (code && BACKFILL_ERROR_MAP[code]) {
        const mapped = BACKFILL_ERROR_MAP[code];
        // If backend included a more specific message, prefer it as the
        // detail under the user-friendly title
        return {
            title: mapped.title,
            hint: backendMessage && backendMessage !== mapped.title
                ? `${mapped.hint ?? ''}${mapped.hint ? ' ' : ''}(${backendMessage})`
                : mapped.hint,
        };
    }

    return {
        title: backendMessage || 'Backfill request failed',
        hint: code ? `Error code: ${code}` : undefined,
    };
}

/**
 * Convenience: format error for `toast.error` (single string).
 */
export function backfillErrorToString(err: unknown): string {
    const info = formatBackfillError(err);
    return info.hint ? `${info.title} — ${info.hint}` : info.title;
}
