import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useAllRunsQuery, useAllTasksQuery } from './useGlobalQueries';

vi.mock('../../utils', () => ({
    api: { get: vi.fn(), post: vi.fn() },
}));

import { api } from '../../utils';

const createWrapper = () => {
    const queryClient = new QueryClient({
        defaultOptions: { queries: { retry: false, gcTime: 0 } },
    });
    return function Wrapper({ children }: { children: React.ReactNode }) {
        return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
    };
};

/** Query string of the nth api.get call, as parsed params. */
const paramsOf = (call = 0) =>
    new URLSearchParams((api.get as ReturnType<typeof vi.fn>).mock.calls[call][0].split('?')[1]);

describe('useAllRunsQuery', () => {
    beforeEach(() => vi.clearAllMocks());

    it('asks for the newest page first — no cursor', async () => {
        api.get.mockResolvedValue({ runs: [{ pipeline_execution: 'p-1' }], next: null });

        const { result } = renderHook(() => useAllRunsQuery('', {}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        expect(paramsOf().has('before')).toBe(false);
        expect(result.current.data?.pages[0].runs).toEqual([{ pipeline_execution: 'p-1' }]);
    });

    it('forwards the filters', async () => {
        api.get.mockResolvedValue({ runs: [], next: null });

        const { result } = renderHook(
            () => useAllRunsQuery('2026-07-16', { status: 'failed', pipeline: 'sales' }),
            { wrapper: createWrapper() },
        );
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        const params = paramsOf();
        expect(params.get('date')).toBe('2026-07-16');
        expect(params.get('status')).toBe('failed');
        expect(params.get('pipeline')).toBe('sales');
    });

    it('knows there is more only when the API hands back a cursor', async () => {
        api.get.mockResolvedValue({ runs: [{ pipeline_execution: 'p-1' }], next: '2026-07-16T10:00:00Z' });

        const { result } = renderHook(() => useAllRunsQuery('', {}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        expect(result.current.hasNextPage).toBe(true);
    });

    it('stops at the end of the feed rather than guessing', async () => {
        api.get.mockResolvedValue({ runs: [{ pipeline_execution: 'p-1' }], next: null });

        const { result } = renderHook(() => useAllRunsQuery('', {}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        expect(result.current.hasNextPage).toBe(false);
    });

    it('sends the cursor back to fetch the older page, and keeps both', async () => {
        api.get
            .mockResolvedValueOnce({ runs: [{ pipeline_execution: 'p-2' }], next: '2026-07-16T10:00:00Z' })
            .mockResolvedValueOnce({ runs: [{ pipeline_execution: 'p-1' }], next: null });

        const { result } = renderHook(() => useAllRunsQuery('', {}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        result.current.fetchNextPage();
        await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));

        expect(paramsOf(1).get('before')).toBe('2026-07-16T10:00:00Z');
        expect(result.current.data?.pages.flatMap(p => p.runs))
            .toEqual([{ pipeline_execution: 'p-2' }, { pipeline_execution: 'p-1' }]);
        expect(result.current.hasNextPage).toBe(false);
    });

    it('surfaces an API error instead of rendering an empty feed', async () => {
        api.get.mockResolvedValue({ error: 'Connection failed' });

        const { result } = renderHook(() => useAllRunsQuery('', {}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isError).toBe(true));

        expect(result.current.error?.message).toBe('Connection failed');
    });
});

describe('useAllTasksQuery', () => {
    beforeEach(() => vi.clearAllMocks());

    it('asks for the newest page first — no cursor', async () => {
        api.get.mockResolvedValue({ tasks: [{ task_name: 'extract' }], next: null });

        const { result } = renderHook(() => useAllTasksQuery({}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        expect(paramsOf().has('before')).toBe(false);
        expect(result.current.data?.pages[0].tasks).toEqual([{ task_name: 'extract' }]);
    });

    it('sends the cursor back to fetch the older page, and keeps both', async () => {
        api.get
            .mockResolvedValueOnce({ tasks: [{ task_name: 'load' }], next: '2026-07-16T10:00:00Z' })
            .mockResolvedValueOnce({ tasks: [{ task_name: 'extract' }], next: null });

        const { result } = renderHook(() => useAllTasksQuery({}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        result.current.fetchNextPage();
        await waitFor(() => expect(result.current.data?.pages).toHaveLength(2));

        expect(paramsOf(1).get('before')).toBe('2026-07-16T10:00:00Z');
        expect(result.current.data?.pages.flatMap(p => p.tasks))
            .toEqual([{ task_name: 'load' }, { task_name: 'extract' }]);
    });

    it('keeps filtering by task name per page (it is not a server filter)', async () => {
        api.get.mockResolvedValue({
            tasks: [{ task_name: 'extract_users' }, { task_name: 'load' }],
            next: null,
        });

        const { result } = renderHook(() => useAllTasksQuery({ taskName: 'extract' }), {
            wrapper: createWrapper(),
        });
        await waitFor(() => expect(result.current.isSuccess).toBe(true));

        expect(result.current.data?.pages[0].tasks).toEqual([{ task_name: 'extract_users' }]);
        expect(paramsOf().has('taskName')).toBe(false);
    });

    it('surfaces an API error instead of rendering an empty feed', async () => {
        api.get.mockResolvedValue({ error: 'Connection failed' });

        const { result } = renderHook(() => useAllTasksQuery({}), { wrapper: createWrapper() });
        await waitFor(() => expect(result.current.isError).toBe(true));

        expect(result.current.error?.message).toBe('Connection failed');
    });
});
