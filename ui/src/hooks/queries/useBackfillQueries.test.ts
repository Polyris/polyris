/**
 * useBackfillsListQuery tests (v0.78+, ADR #51).
 *
 * The public list query that powers the Header notification badge. The
 * Team-tier start/preview/detail/cancel/retry hooks (and their tests) moved to
 * src/ee/team/hooks/queries/useBackfillQueries.test.ts (ADR #99). Mocks the `api`
 * module at the boundary (per CLAUDE.md #14) so the hook logic runs real
 * React Query under test.
 */

import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// ── Boundary mock (per CLAUDE.md #14) ────────────────────────────────────────

const { mockApi } = vi.hoisted(() => ({
    mockApi: {
        get: vi.fn(),
        post: vi.fn(),
        put: vi.fn(),
        delete: vi.fn(),
    },
}));

vi.mock('../../utils', () => ({
    api: mockApi,
}));

import { useBackfillsListQuery } from './useBackfillQueries';

function wrapper(qc: QueryClient) {
    const Wrapper = ({ children }: { children: React.ReactNode }) =>
        React.createElement(QueryClientProvider, { client: qc }, children);
    Wrapper.displayName = 'TestQueryClientWrapper';
    return Wrapper;
}

function makeQc() {
    return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

describe('useBackfillsListQuery', () => {
    beforeEach(() => {
        mockApi.get.mockReset();
        mockApi.post.mockReset();
        mockApi.put.mockReset();
        mockApi.delete.mockReset();
    });

    it('fetches /api/backfills with no filter when null', async () => {
        mockApi.get.mockResolvedValue({ ok: true, data: { backfills: [], count: 0 } });
        const qc = makeQc();
        const { result } = renderHook(() => useBackfillsListQuery(null), { wrapper: wrapper(qc) });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));
        expect(mockApi.get).toHaveBeenCalled();
        const path = mockApi.get.mock.calls[0][0];
        expect(path).toMatch(/^\/backfills/);
        expect(path).not.toMatch(/status=/);
    });

    it('adds status filter to query string when provided', async () => {
        mockApi.get.mockResolvedValue({ ok: true, data: { backfills: [], count: 0 } });
        const qc = makeQc();
        const { result } = renderHook(() => useBackfillsListQuery('failed'), { wrapper: wrapper(qc) });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));
        const path = mockApi.get.mock.calls[0][0];
        expect(path).toMatch(/status=failed/);
    });
});
