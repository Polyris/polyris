/**
 * The System status rule (contract §3): /api/health as loading | unknown | attention | healthy.
 * The api boundary is mocked; the rule and the hook run for real. The answers below follow
 * the shapes routes/health.py writes, and the parity tests at the end pin the words.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { readFileSync } from 'fs';
import { resolve } from 'path';

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }));

vi.mock('../../utils/api', async (importOriginal) => ({
    ...(await importOriginal<typeof import('../../utils/api')>()),
    api: { get: mockGet },
}));

import {
    HEALTH_DETAILS,
    HEALTH_WORDS,
    LOST_CONNECTION_REASON,
    API_ERROR_REASON,
    SYSTEM_CHECKS,
    SYSTEM_FINDING_FIELDS,
    readSystemStatus,
    useSystemStatusQuery,
} from './useSystemStatusQuery';
import {
    HEALTH_CHECKED_AT,
    apiAnswered,
    apiFailed,
    createFinding,
    createHealthBody,
    createMinimalHealthBody,
} from '../../test/factories';

// ─── Answers, as api.get returns them ────────────────────────────────────────

const MAPPING_OFF = createFinding();

// ─── The rule ────────────────────────────────────────────────────────────────

describe('readSystemStatus', () => {
    it('is healthy when coordination is healthy and both services healthy', () => {
        expect(readSystemStatus(apiAnswered(createHealthBody()))).toEqual({ kind: 'healthy', detail: 'full', checkedAt: HEALTH_CHECKED_AT });
    });

    it('ignores degraded (latency), recent failures, circuit breakers and the top-level status', () => {
        const body = createHealthBody({ dynamodb: 'degraded', stepfunctions: 'degraded' });
        const noisy = {
            ...body,
            status: 'unhealthy',
            checks: { ...body.checks, recent_failures: { status: 'unhealthy' }, circuit_breakers: { status: 'unhealthy' } },
        };
        expect(readSystemStatus(apiAnswered(noisy)).kind).toBe('healthy');
    });

    it('needs attention when coordination is unhealthy, with the findings', () => {
        const status = readSystemStatus(apiAnswered(createHealthBody({ coordination: 'unhealthy' }, [MAPPING_OFF])));
        expect(status).toEqual({
            kind: 'attention', detail: 'full', unhealthy: ['Task coordination'], findings: [MAPPING_OFF], checkedAt: HEALTH_CHECKED_AT,
        });
    });

    it('names a failed answer by its body\'s message, not its error code', () => {
        const body = { error: 'INTERNAL_ERROR', message: 'Internal error; the console API logs have the details.' };
        expect(readSystemStatus(apiFailed('INTERNAL_ERROR', 500, body))).toEqual(
            { kind: 'unknown', reason: 'Internal error; the console API logs have the details' });
    });

    it('reads a 503 as an answer: attention with the body\'s findings', () => {
        const reach = { title: 'The console cannot reach DynamoDB', resource: 'AccessDeniedException: …', impact: 'The console cannot read or change pipelines and tasks.' };
        const body = { ...createHealthBody({ dynamodb: 'unhealthy', stepfunctions: 'unhealthy' }, [reach]), status: 'unhealthy' };
        const status = readSystemStatus(apiFailed('HTTP 503', 503, body));
        expect(status).toMatchObject({ kind: 'attention', detail: 'full', unhealthy: ['DynamoDB', 'Step Functions'], findings: [reach] });
    });

    it('needs attention on one unhealthy word even when another is missing', () => {
        const body = createHealthBody({ stepfunctions: 'unhealthy' });
        delete (body.checks as Partial<typeof body.checks>).coordination;
        expect(readSystemStatus(apiAnswered(body))).toMatchObject({ kind: 'attention', unhealthy: ['Step Functions'] });
    });

    it('reads the minimal answer from the words alone: no findings, no check time', () => {
        expect(readSystemStatus(apiAnswered(createMinimalHealthBody({ coordination: 'unhealthy' }))))
            .toEqual({ kind: 'attention', detail: 'minimal', unhealthy: ['Task coordination'], findings: [], checkedAt: null });
        expect(readSystemStatus(apiAnswered(createMinimalHealthBody()))).toEqual({ kind: 'healthy', detail: 'minimal', checkedAt: null });
    });

    it('never reads findings from an answer that does not say it is full', () => {
        const body = { ...createHealthBody({ coordination: 'unhealthy' }, [MAPPING_OFF]), detail: undefined };
        expect(readSystemStatus(apiAnswered(body))).toMatchObject({ kind: 'attention', detail: 'minimal', findings: [], checkedAt: null });
    });

    it('is unknown when the request failed without a body, naming the error', () => {
        expect(readSystemStatus(apiFailed('Failed to fetch'))).toEqual({ kind: 'unknown', reason: 'Failed to fetch' });
        expect(readSystemStatus(apiFailed('HTTP 502', 502))).toEqual({ kind: 'unknown', reason: 'HTTP 502' });
        expect(readSystemStatus(apiFailed('Endpoint request timed out.', 504))).toEqual({ kind: 'unknown', reason: 'Endpoint request timed out' });
        expect(readSystemStatus({ ok: false })).toEqual({ kind: 'unknown', reason: 'the request failed' });
        expect(readSystemStatus(null)).toEqual({ kind: 'unknown', reason: 'the request failed' });
    });

    it('is unknown for an error body without the words (a gateway error page)', () => {
        expect(readSystemStatus(apiFailed('Internal server error', 502, { message: 'Internal server error' })))
            .toEqual({ kind: 'unknown', reason: 'Internal server error' });
    });

    it('is unknown, never healthy, when a word the rule needs is missing', () => {
        const body = createHealthBody();
        delete (body.checks as Partial<typeof body.checks>).coordination;
        expect(readSystemStatus(apiAnswered(body))).toEqual({ kind: 'unknown', reason: 'the answer has no known status for Task coordination' });
        expect(readSystemStatus(apiAnswered({ status: 'healthy' })))
            .toEqual({ kind: 'unknown', reason: 'the answer has no known status for DynamoDB, Step Functions, Task coordination' });
    });

    it('is unknown, never healthy, when a word is not recognised', () => {
        expect(readSystemStatus(apiAnswered(createHealthBody({ dynamodb: 'ok' })))).toEqual({ kind: 'unknown', reason: 'the answer has no known status for DynamoDB' });
        expect(readSystemStatus(apiAnswered(createHealthBody({ stepfunctions: 42 }))).kind).toBe('unknown');
        // Coordination is healthy or unhealthy; degraded is a services-only word.
        expect(readSystemStatus(apiAnswered(createHealthBody({ coordination: 'degraded' })))).toEqual({ kind: 'unknown', reason: 'the answer has no known status for Task coordination' });
    });

    it('keeps only the string fields of a finding, and skips (and says so) one without a title', () => {
        const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
        const status = readSystemStatus(apiAnswered(createHealthBody({ coordination: 'unhealthy' }, [
            { title: 'Waiting tasks are behind their checks', resource: 'The oldest check was due 52 min ago.', command: '', fix: 7 },
            { resource: 'no title' },
            'not a finding',
        ])));
        expect(status).toMatchObject({ kind: 'attention', findings: [{ title: 'Waiting tasks are behind their checks', resource: 'The oldest check was due 52 min ago.' }] });
        expect(status.kind === 'attention' && status.findings[0]).toEqual({ title: 'Waiting tasks are behind their checks', resource: 'The oldest check was due 52 min ago.' });
        expect(warn).toHaveBeenCalledTimes(2);
    });
});

// ─── The hook ────────────────────────────────────────────────────────────────

function wrapper() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const Wrapper = ({ children }: { children: React.ReactNode }) =>
        React.createElement(QueryClientProvider, { client }, children);
    Wrapper.displayName = 'TestQueryClientWrapper';
    return Wrapper;
}

describe('useSystemStatusQuery', () => {
    beforeEach(() => mockGet.mockReset());

    it('is loading until the first answer, then reads /api/health', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody()));
        const { result } = renderHook(() => useSystemStatusQuery(null), { wrapper: wrapper() });
        expect(result.current.status).toEqual({ kind: 'loading' });
        await waitFor(() => expect(result.current.status.kind).toBe('healthy'));
        expect(mockGet).toHaveBeenCalledWith('/health');
    });

    it('shows a failure after an earlier success as unknown, not the old answer', async () => {
        mockGet.mockResolvedValueOnce(apiAnswered(createHealthBody())).mockResolvedValueOnce(apiFailed('HTTP 502', 502));
        const { result } = renderHook(() => useSystemStatusQuery(null), { wrapper: wrapper() });
        await waitFor(() => expect(result.current.status.kind).toBe('healthy'));
        await act(async () => { await result.current.refetch(); });
        await waitFor(() => expect(result.current.status).toEqual({ kind: 'unknown', reason: 'HTTP 502' }));
        expect(mockGet).toHaveBeenCalledTimes(2);
    });

    it('keeps an answer that names a problem when the console loses its API: the 503 says why', async () => {
        const reach = createFinding({ title: 'The console cannot reach DynamoDB' });
        const body = { ...createHealthBody({ dynamodb: 'unhealthy' }, [reach]), status: 'unhealthy' };
        mockGet.mockResolvedValue(apiFailed('HTTP 503', 503, body));
        const lost = { unreachable: false, message: 'The API hit a server error. Please try again in a moment.' };
        const { result } = renderHook(() => useSystemStatusQuery(lost), { wrapper: wrapper() });
        await waitFor(() => expect(result.current.status.kind).toBe('attention'));
        expect(result.current.status).toMatchObject({ unhealthy: ['DynamoDB'], findings: [reach], connection: lost.message });
    });

    it('is a connection problem while the pipelines read fails, when the last answer was green, and says which way', async () => {
        mockGet.mockResolvedValue(apiAnswered(createHealthBody()));
        const gone = { unreachable: true, message: 'Could not reach the API.' };
        const erring = { unreachable: false, message: 'You are not authorized to make this request.' };
        const { result, rerender } = renderHook(({ error }) => useSystemStatusQuery(error), {
            wrapper: wrapper(), initialProps: { error: null as typeof gone | null },
        });
        await waitFor(() => expect(result.current.status.kind).toBe('healthy'));
        rerender({ error: gone });
        expect(result.current.status).toEqual({ kind: 'connection', pill: 'no connection', reason: LOST_CONNECTION_REASON, detail: gone.message });
        rerender({ error: erring });
        expect(result.current.status).toEqual({ kind: 'connection', pill: 'API error', reason: API_ERROR_REASON, detail: erring.message });
        rerender({ error: null });
        expect(result.current.status.kind).toBe('healthy');
    });

    it('is unknown, not stuck loading, when the client answers nothing at all', async () => {
        mockGet.mockResolvedValue(undefined);
        const { result } = renderHook(() => useSystemStatusQuery(null), { wrapper: wrapper() });
        await waitFor(() => expect(result.current.status).toEqual({ kind: 'unknown', reason: 'the request failed' }));
    });
});

// ─── Parity with the backend (Principle #28) ─────────────────────────────────

describe('parity with the console API', () => {
    // process.cwd() is ui/ under vitest; the backend sits one level up.
    const health = readFileSync(resolve(process.cwd(), '../sam/lambdas/console_api/routes/health.py'), 'utf-8');
    const sweep = readFileSync(resolve(process.cwd(), '../polyris/coordination/sweep.py'), 'utf-8');

    const pythonConstant = (name: string): string => {
        const match = health.match(new RegExp(`^${name}\\s*=\\s*['"]([^'"]+)['"]`, 'm'));
        expect(match, `${name} not found in routes/health.py`).not.toBeNull();
        return match![1];
    };

    it.each(Object.entries(HEALTH_WORDS))('the status word %s is routes/health.py\'s', (name, word) => {
        expect(word).toBe(pythonConstant(name));
    });

    it.each(Object.entries(HEALTH_DETAILS))('the detail word %s is routes/health.py\'s', (name, word) => {
        expect(word).toBe(pythonConstant(name));
    });

    it.each(SYSTEM_CHECKS.map(check => check.name))('routes/health.py answers a %s check', (name) => {
        // An assignment to checks['<name>'], alone or in a tuple: `checks['x'] = …` or `checks['x'], a = …`.
        expect(health).toMatch(new RegExp(`checks\\[['"]${name}['"]\\][^=\\n]*=(?!=)`));
    });

    it('a finding has the fields sweep._finding writes', () => {
        const fields = sweep.match(/def _finding\([\s\S]*?fields = \{([\s\S]*?)\}/);
        expect(fields, '_finding fields not found in polyris/coordination/sweep.py').not.toBeNull();
        expect([...fields![1].matchAll(/["'](\w+)["']\s*:/g)].map(m => m[1])).toEqual([...SYSTEM_FINDING_FIELDS]);
    });
});
