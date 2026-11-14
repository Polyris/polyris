import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../../utils';
import { asRecord } from '../../utils/formatters';
import { logger } from '../../utils/logger';
import { queryKeys } from '../../lib/queryClient';

/**
 * The System status: the console's reading of GET /api/health, shown by the pill in
 * the header (components/SystemStatus.tsx).
 *
 * The rule reads three status words and nothing else:
 *   - attention: DynamoDB, Step Functions or Task coordination is `unhealthy`;
 *   - healthy:   Task coordination is `healthy`, and DynamoDB and Step Functions are
 *                each `healthy` or `degraded` (`degraded` is DynamoDB latency only);
 *   - unknown:   everything else. A failed request without a body, a lost connection
 *                or an answer without the words the rule needs is never shown as healthy.
 *
 * It never reads list lengths: the minimal answer (no valid token) has no findings at
 * all. `recent_failures`, `circuit_breakers` and the top-level `status` are ignored;
 * task failures belong to the bell.
 */

/** The status words of `routes/health.py` (`HEALTHY`, `DEGRADED`, `UNHEALTHY`), pinned by a parity test. */
export const HEALTH_WORDS = {
    HEALTHY: 'healthy',
    DEGRADED: 'degraded',
    UNHEALTHY: 'unhealthy',
} as const;

/** The checks the rule reads, in the order the panel lists them. The names are pinned by the parity test. */
export const SYSTEM_CHECKS = [
    { name: 'dynamodb', label: 'DynamoDB' },
    { name: 'stepfunctions', label: 'Step Functions' },
    { name: 'coordination', label: 'Task coordination' },
] as const;

/** The `detail` words of `routes/health.py` (`FULL`, `MINIMAL`), pinned by the parity test. */
export const HEALTH_DETAILS = {
    FULL: 'full',
    MINIMAL: 'minimal',
} as const;

/** A finding's fields, in the order `sweep._finding` writes them; pinned by the parity test. */
export const SYSTEM_FINDING_FIELDS = ['title', 'resource', 'changed_at', 'impact', 'fix', 'command'] as const;

/** How often the console reads /api/health. The unknown panel names it. */
export const SYSTEM_STATUS_POLL_MINUTES = 2;

/** The unknown reason while the console cannot reach its API at all (the "Connection lost" banner shows too). */
export const LOST_CONNECTION_REASON = 'the console has lost its connection to the API';

/**
 * One problem, in final English: the panel shows each field as is. Only `title` is always
 * present. `changed_at` is ISO UTC (when AWS last changed the resource); `command` holds
 * shell lines separated by `\n`.
 */
export type SystemFinding = { title: string } & Partial<Record<Exclude<typeof SYSTEM_FINDING_FIELDS[number], 'title'>, string>>;

/** `full` with a valid token (or with auth off); `minimal` (status words only) otherwise. */
export type SystemStatusDetail = typeof HEALTH_DETAILS[keyof typeof HEALTH_DETAILS];

export type SystemStatusState =
    | { kind: 'loading' }
    /** `reason` is a phrase without a final period, shown mid-sentence. */
    | { kind: 'unknown'; reason: string }
    /** `unhealthy` holds the labels of the unhealthy checks; `findings` is empty in the minimal view. */
    | { kind: 'attention'; detail: SystemStatusDetail; unhealthy: string[]; findings: SystemFinding[]; checkedAt: string | null }
    | { kind: 'healthy'; detail: SystemStatusDetail; checkedAt: string | null };

const text = (value: unknown): string | undefined =>
    typeof value === 'string' && value.trim() !== '' ? value : undefined;

function readFindings(list: unknown): SystemFinding[] {
    if (!Array.isArray(list)) return [];
    const findings: SystemFinding[] = [];
    for (const item of list) {
        const raw = asRecord(item);
        const title = text(raw.title);
        if (!title) {
            logger.warn('system-status', 'Skipped a finding without a title', item);
            continue;
        }
        const finding: SystemFinding = { title };
        for (const field of SYSTEM_FINDING_FIELDS) {
            const value = text(raw[field]);
            if (field !== 'title' && value) finding[field] = value;
        }
        findings.push(finding);
    }
    return findings;
}

/**
 * Turn one `api.get('/health')` result into a state. `api.get` answers `{ ok: true, data }`
 * for a 2xx and `{ ok: false, error, status?, body? }` otherwise; a 503 is an answer too,
 * and its body carries the checks and findings.
 */
export function readSystemStatus(result: unknown): Exclude<SystemStatusState, { kind: 'loading' }> {
    const answer = asRecord(result);
    const body = asRecord(answer.ok === true ? answer.data : answer.body);
    const checks = asRecord(body.checks);
    const words = SYSTEM_CHECKS.map(check => ({ ...check, word: asRecord(checks[check.name]).status }));
    // Only an answer that says it is full is read for findings; anything else shows the checks alone.
    const detail: SystemStatusDetail = body.detail === HEALTH_DETAILS.FULL ? HEALTH_DETAILS.FULL : HEALTH_DETAILS.MINIMAL;
    const full = detail === HEALTH_DETAILS.FULL;
    const checkedAt = full ? text(asRecord(asRecord(checks.coordination).report).checked_at) ?? null : null;

    const unhealthy = words.filter(check => check.word === HEALTH_WORDS.UNHEALTHY).map(check => check.label);
    if (unhealthy.length > 0) {
        return { kind: 'attention', detail, unhealthy, findings: full ? readFindings(body.findings) : [], checkedAt };
    }

    const unread = words.filter(check => !(check.name === 'coordination'
        ? check.word === HEALTH_WORDS.HEALTHY
        : check.word === HEALTH_WORDS.HEALTHY || check.word === HEALTH_WORDS.DEGRADED));
    if (unread.length === 0) return { kind: 'healthy', detail, checkedAt };

    // The default branch: no usable answer. Never green.
    const reason = answer.ok === true
        ? `the answer has no known status for ${unread.map(check => check.label).join(', ')}`
        // A failed answer's body says more than its error code (the catch-all sends `INTERNAL_ERROR`).
        : text(body.message) ?? text(answer.error) ?? 'the request failed';
    return { kind: 'unknown', reason: reason.replace(/\.$/, '') };
}

/**
 * useSystemStatusQuery - /api/health as a tagged state, polled every 2 minutes. The sweeper
 * reports every 5 minutes and the backend judges its staleness, so a stopped sweeper shows
 * too; polling faster would only add DynamoDB and Step Functions calls.
 *
 * `apiConnected` is false while the console cannot reach its API (any failed pipelines read).
 * It turns a green or pending state into unknown, which can no longer be trusted. An answer
 * that names a problem stays: when DynamoDB is down the pipelines read fails too, and the 503
 * from /api/health says why (architect review, 1.2.0).
 */
export function useSystemStatusQuery(apiConnected: boolean) {
    const { data, refetch, isFetching } = useQuery({
        queryKey: queryKeys.systemStatus,
        // api.get does not throw: a failure is a result like any other, so it replaces the answer
        // before it instead of leaving that answer on show. React Query rejects undefined data.
        queryFn: async (): Promise<unknown> => (await api.get('/health')) ?? null,
        staleTime: 60 * 1000,
        refetchInterval: SYSTEM_STATUS_POLL_MINUTES * 60 * 1000,
    });

    const status = useMemo<SystemStatusState>(() => {
        const read = data === undefined ? null : readSystemStatus(data);
        if (read?.kind === 'attention') return read;
        if (!apiConnected) return { kind: 'unknown', reason: LOST_CONNECTION_REASON };
        return read ?? { kind: 'loading' };
    }, [apiConnected, data]);

    return { status, refetch, isFetching };
}
