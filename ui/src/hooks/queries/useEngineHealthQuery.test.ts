/**
 * useEngineHealthQuery reads the coordination section of /api/health (ADR #130 §9).
 * The api boundary is mocked; the hook's own parsing runs for real.
 */
import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const { mockApi } = vi.hoisted(() => ({ mockApi: { get: vi.fn() } }));

vi.mock('../../utils', () => ({
    api: mockApi,
}));

import { useEngineHealthQuery } from './useGlobalQueries';

function wrapper(qc: QueryClient) {
    const Wrapper = ({ children }: { children: React.ReactNode }) =>
        React.createElement(QueryClientProvider, { client: qc }, children);
    Wrapper.displayName = 'TestQueryClientWrapper';
    return Wrapper;
}

describe('useEngineHealthQuery', () => {
    beforeEach(() => mockApi.get.mockReset());

    it('returns the problems the health check reports', async () => {
        mockApi.get.mockResolvedValue({ checks: { coordination: { status: 'unhealthy', problems: ['stale'] } } });
        const { result } = renderHook(() => useEngineHealthQuery(), { wrapper: wrapper(new QueryClient()) });
        await waitFor(() => expect(result.current.data).toEqual({ problems: ['stale'] }));
        expect(mockApi.get).toHaveBeenCalledWith('/health');
    });

    it('reports no problems when the health check reports none', async () => {
        mockApi.get.mockResolvedValue({ checks: { coordination: { status: 'healthy', problems: [] } } });
        const { result } = renderHook(() => useEngineHealthQuery(), { wrapper: wrapper(new QueryClient()) });
        await waitFor(() => expect(result.current.data).toEqual({ problems: [] }));
    });

    // Reading "no section" as "no problems" hid the banner exactly when /api/health failed (1.2.0 review).
    it('says health cannot be read when the section is missing', async () => {
        mockApi.get.mockResolvedValue({ checks: {} });
        const { result } = renderHook(() => useEngineHealthQuery(), { wrapper: wrapper(new QueryClient()) });
        await waitFor(() => expect(result.current.data?.problems[0]).toMatch(/^Task coordination health cannot be read/));
    });

    it('says health cannot be read when the request fails', async () => {
        mockApi.get.mockResolvedValue({ ok: false, error: 'HTTP 503', status: 503 });
        const { result } = renderHook(() => useEngineHealthQuery(), { wrapper: wrapper(new QueryClient()) });
        await waitFor(() => expect(result.current.data?.problems).toEqual(['Task coordination health cannot be read: HTTP 503']));
    });
});
