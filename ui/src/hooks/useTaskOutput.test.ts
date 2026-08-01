import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useTaskOutput } from './useTaskOutput';

const get = vi.fn();
vi.mock('../utils', () => ({ api: { get: (...args: unknown[]) => get(...args) } }));

const task = {
    task_name: 'extract',
    execution_name: 'extract-2026-07-07-abc',
    date: '2026-07-07',
    pipeline_execution: 'p-run-2026-07-07-abc',
};

describe('useTaskOutput', () => {
    beforeEach(() => get.mockReset());

    it('does not fetch while inactive', () => {
        renderHook(() => useTaskOutput(task, false));
        expect(get).not.toHaveBeenCalled();
    });

    it('does not fetch without a task name', () => {
        renderHook(() => useTaskOutput({ date: '2026-07-07' }, true));
        expect(get).not.toHaveBeenCalled();
    });

    it('fetches and returns input + output when active', async () => {
        get.mockResolvedValueOnce({
            input: { variables: { year: '2026' } },
            output: { rows: 5 },
            truncated: false,
        });
        const { result } = renderHook(() => useTaskOutput(task, true));
        await waitFor(() => expect(result.current.loaded).toBe(true));
        expect(result.current.output).toEqual({ rows: 5 });
        expect(result.current.input).toEqual({ variables: { year: '2026' } });
        expect(result.current.truncated).toBe(false);
        const url = get.mock.calls[0][0] as string;
        expect(url).toContain('/task-output?');
        expect(url).toContain('name=extract-2026-07-07-abc');
        expect(url).toContain('date=2026-07-07');
    });

    it('flags a truncated output', async () => {
        get.mockResolvedValueOnce({ output: null, truncated: true });
        const { result } = renderHook(() => useTaskOutput(task, true));
        await waitFor(() => expect(result.current.loaded).toBe(true));
        expect(result.current.truncated).toBe(true);
    });

    it('handles a fetch error gracefully', async () => {
        vi.spyOn(console, 'error').mockImplementation(() => {});
        get.mockRejectedValueOnce(new Error('boom'));
        const { result } = renderHook(() => useTaskOutput(task, true));
        await waitFor(() => expect(result.current.loaded).toBe(true));
        expect(result.current.output).toBeNull();
    });
});
