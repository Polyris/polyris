import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useUrlSync } from './useUrlSync';

interface TestState {
    pipeline?: string;
    mode?: string;
    date?: string;
    execution?: string;
}

const KEYS = ['pipeline', 'mode', 'date', 'execution'] as const;
const DEFAULTS = { mode: 'dag' };

describe('useUrlSync', () => {
    let pushStateSpy: ReturnType<typeof vi.spyOn>;
    let replaceStateSpy: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
        window.history.replaceState({}, '', '/pipelines/');
        pushStateSpy = vi.spyOn(window.history, 'pushState');
        replaceStateSpy = vi.spyOn(window.history, 'replaceState');
    });

    afterEach(() => {
        pushStateSpy.mockRestore();
        replaceStateSpy.mockRestore();
    });

    describe('parseUrl / initialState', () => {
        it('returns empty state for clean URL', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            expect(result.current.initialState).toEqual({});
        });

        it('parses pipeline param', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            expect(result.current.initialState).toEqual({ pipeline: 'etl_main' });
        });

        it('parses all params', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main&mode=gantt&date=2025-01-15&execution=exec-123');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            expect(result.current.initialState).toEqual({
                pipeline: 'etl_main',
                mode: 'gantt',
                date: '2025-01-15',
                execution: 'exec-123',
            });
        });

        it('ignores unknown params (not in keys)', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main&foo=bar');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            expect(result.current.initialState).toEqual({ pipeline: 'etl_main' });
        });

        it('respects custom keys (route-specific scoping)', () => {
            interface AssetsState { asset?: string; tab?: string }
            const ASSETS_KEYS = ['asset', 'tab'] as const;
            window.history.replaceState({}, '', '/assets/?asset=acme/foo&tab=schema&pipeline=ignored');
            const { result } = renderHook(() => useUrlSync<AssetsState>({ keys: ASSETS_KEYS }));
            expect(result.current.initialState).toEqual({ asset: 'acme/foo', tab: 'schema' });
        });
    });

    describe('updateUrl', () => {
        it('pushes state to URL preserving pathname', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS, defaults: DEFAULTS }));
            act(() => {
                result.current.updateUrl({ pipeline: 'etl_main' });
            });
            expect(pushStateSpy).toHaveBeenCalledWith(
                { pipeline: 'etl_main' },
                '',
                '/pipelines/?pipeline=etl_main'
            );
        });

        it('omits default mode (dag) from URL', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS, defaults: DEFAULTS }));
            act(() => {
                result.current.updateUrl({ pipeline: 'etl_main', mode: 'dag' });
            });
            expect(pushStateSpy).toHaveBeenCalledWith(
                { pipeline: 'etl_main', mode: 'dag' },
                '',
                '/pipelines/?pipeline=etl_main'
            );
        });

        it('keeps non-default mode', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS, defaults: DEFAULTS }));
            act(() => {
                result.current.updateUrl({ pipeline: 'etl_main', mode: 'gantt' });
            });
            expect(pushStateSpy).toHaveBeenCalledWith(
                { pipeline: 'etl_main', mode: 'gantt' },
                '',
                '/pipelines/?pipeline=etl_main&mode=gantt'
            );
        });

        it('does not push duplicate state', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS, defaults: DEFAULTS }));
            act(() => {
                result.current.updateUrl({ pipeline: 'etl_main' });
            });
            expect(pushStateSpy).not.toHaveBeenCalled();
        });

        it('respects pathname for /assets/ route', () => {
            window.history.replaceState({}, '', '/assets/');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            act(() => {
                result.current.updateUrl({ pipeline: 'etl_main' });
            });
            expect(pushStateSpy).toHaveBeenCalledWith(
                { pipeline: 'etl_main' },
                '',
                '/assets/?pipeline=etl_main'
            );
        });

        it('skips empty string values', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            act(() => {
                result.current.updateUrl({ pipeline: '', date: '2025-01-01' });
            });
            expect(pushStateSpy).toHaveBeenCalledWith(
                { pipeline: '', date: '2025-01-01' },
                '',
                '/pipelines/?date=2025-01-01'
            );
        });
    });

    describe('replaceUrl', () => {
        it('replaces state without new history entry', () => {
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            act(() => {
                result.current.replaceUrl({ pipeline: 'etl_main', date: '2025-06-01' });
            });
            expect(replaceStateSpy).toHaveBeenCalledWith(
                { pipeline: 'etl_main', date: '2025-06-01' },
                '',
                '/pipelines/?pipeline=etl_main&date=2025-06-01'
            );
        });

        it('clears query string when state is empty', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main');
            const { result } = renderHook(() => useUrlSync<TestState>({ keys: KEYS }));
            act(() => {
                result.current.replaceUrl({});
            });
            expect(replaceStateSpy).toHaveBeenCalledWith(
                {},
                '',
                '/pipelines/'
            );
        });
    });

    describe('popstate / back-forward', () => {
        it('calls onChange on popstate event', () => {
            window.history.replaceState({}, '', '/pipelines/?pipeline=etl_main');
            const onChange = vi.fn();
            renderHook(() => useUrlSync<TestState>({ keys: KEYS, onChange }));

            act(() => {
                window.dispatchEvent(new PopStateEvent('popstate'));
            });

            expect(onChange).toHaveBeenCalledWith({ pipeline: 'etl_main' });
        });

        it('cleans up popstate listener on unmount', () => {
            const onChange = vi.fn();
            const { unmount } = renderHook(() => useUrlSync<TestState>({ keys: KEYS, onChange }));
            unmount();

            act(() => {
                window.dispatchEvent(new PopStateEvent('popstate'));
            });
            expect(onChange).not.toHaveBeenCalled();
        });
    });
});
