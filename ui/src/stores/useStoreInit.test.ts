/**
 * useStoreInit tests (v0.78.7).
 *
 * Pins the partition-click date routing bug (ADR #63, fixed v0.78.7).
 *
 * Bug scenario:
 *   1. User on /backfills/bf-X clicks partition cell for 2026-05-22
 *   2. router.push('/pipelines/?pipeline=A&date=2026-05-22')
 *   3. App mounts on the new route. useStoreInit fires.
 *   4. Without the isInitialized gate, the push effect would run with
 *      a STALE store.date (today, from previous session) and STRIP the
 *      date param from the URL via pushState before mount-once could
 *      apply urlState.date.
 *   5. Result: URL flickers to /pipelines/?pipeline=A (no date), pipeline
 *      detail mounts during the gap, fetches today's executions. User
 *      reports "redirected to today instead of partition's date".
 *
 * These tests use a fake URL via jsdom's history API and verify:
 *   - After mount: store.date matches the URL date param
 *   - replaceUrl/pushState is NOT called with a URL missing the date
 *     before the URL→store sync completes
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useStoreInit } from './useStoreInit';
import { useAppStore } from './useAppStore';

// ── Mocks ────────────────────────────────────────────────────────────────────

const mockRouterPush = vi.fn();
let mockPathname = '/pipelines/';

vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: mockPathname, push: mockRouterPush, replace: vi.fn() }),
}));

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Reset jsdom's window.location.search to the given value. */
function setLocationSearch(search: string) {
    const url = new URL(window.location.href);
    url.search = search;
    window.history.replaceState({}, '', url.toString());
}

/** Reset store + window state to a known baseline. */
function resetWorld() {
    setLocationSearch('');
    mockPathname = '/pipelines/';
    mockRouterPush.mockClear();
    // Reset Zustand store
    const store = useAppStore.getState();
    store.setDate('2000-01-01');  // sentinel; not today
    store.setSelectedPipeline(null);
    store.setSelectedExecution(null);
    store.setSelectedTaskName(null);
    store.setViewMode('dag');
}

beforeEach(() => {
    resetWorld();
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('useStoreInit › date initialization from URL', () => {
    it('applies URL date param to store on mount', () => {
        // Arrange: URL says ?date=2026-05-22
        setLocationSearch('?pipeline=acme-daily&date=2026-05-22');

        // Act: mount the hook (mimics App.tsx mounting on /pipelines/)
        renderHook(() => useStoreInit({ pipelines: [] }));

        // Assert: store.date is the URL date, NOT the sentinel
        expect(useAppStore.getState().date).toBe('2026-05-22');
    });

    it('falls back to today when URL has no date param', () => {
        setLocationSearch('?pipeline=acme-daily');

        renderHook(() => useStoreInit({ pipelines: [] }));

        // Today's date in YYYY-MM-DD form
        const today = new Date().toISOString().slice(0, 10);
        expect(useAppStore.getState().date).toBe(today);
    });

    it('applies URL mode param to store on mount', () => {
        setLocationSearch('?pipeline=acme-daily&mode=gantt');

        renderHook(() => useStoreInit({ pipelines: [] }));

        expect(useAppStore.getState().viewMode).toBe('gantt');
    });

    // v0.78.7 — regression pin for the partition-click bug (ADR #63).
    //
    // The bug: push/replace URL-sync effects fired BEFORE mount-once
    // had a chance to set store.date from URL. They saw the stale
    // store.date (today) and called pushState with the date param
    // stripped. The fix: gate push/replace on `isInitialized` state.
    //
    // This test pins that gate. Without it, mounting on /pipelines/
    // with `?date=Y` while store.date holds a stale value would
    // produce a URL without the date param within the first commit.
    it('preserves URL date param when store has stale date on mount (ADR #63 regression)', () => {
        // Arrange: window.history has a date param; store still holds
        // a STALE date from a previous session (sentinel value).
        setLocationSearch('?pipeline=acme-daily&date=2026-05-22');
        expect(useAppStore.getState().date).toBe('2000-01-01');  // stale

        // Track pushState calls to ensure the URL date is NEVER stripped
        const pushSpy = vi.spyOn(window.history, 'pushState');
        const replaceSpy = vi.spyOn(window.history, 'replaceState');

        // Act: mount the hook
        renderHook(() => useStoreInit({ pipelines: [] }));

        // Inspect ALL pushState/replaceState calls during mount:
        // none of them should produce a URL that has `?pipeline=acme-daily`
        // but NO date param. That's the bug signature.
        const allUrls = [
            ...pushSpy.mock.calls.map((c) => c[2] as string),
            ...replaceSpy.mock.calls.map((c) => c[2] as string),
        ];
        for (const url of allUrls) {
            if (typeof url !== 'string') continue;
            if (url.includes('pipeline=acme-daily') && !url.includes('date=')) {
                throw new Error(
                    `URL had pipeline but missing date param — partition-click ` +
                    `race regressed: ${url}`,
                );
            }
        }

        // Final state: store.date should match URL
        expect(useAppStore.getState().date).toBe('2026-05-22');

        pushSpy.mockRestore();
        replaceSpy.mockRestore();
    });

    it('does NOT touch URL when not on /pipelines route', () => {
        mockPathname = '/backfills/';
        setLocationSearch('');

        const pushSpy = vi.spyOn(window.history, 'pushState');
        const replaceSpy = vi.spyOn(window.history, 'replaceState');

        renderHook(() => useStoreInit({ pipelines: [] }));

        // No URL writes should happen on non-pipelines routes,
        // because both push/replace effects are gated on onPipelinesRoute.
        expect(pushSpy).not.toHaveBeenCalled();
        expect(replaceSpy).not.toHaveBeenCalled();

        pushSpy.mockRestore();
        replaceSpy.mockRestore();
    });
});

describe('useStoreInit › pipeline restoration', () => {
    it('restores selectedPipeline from URL pipeline param when pipelines load', () => {
        setLocationSearch('?pipeline=acme-daily');

        const pipelines = [
            { name: 'acme-daily', display_name: 'ACME Daily' },
            { name: 'shopmart-weekly', display_name: 'Shopmart Weekly' },
        ] as any[];

        // First render: empty pipelines (loading)
        const { rerender } = renderHook(
            ({ pipelines }) => useStoreInit({ pipelines }),
            { initialProps: { pipelines: [] as any[] } },
        );

        // Rerender: pipelines loaded
        act(() => {
            rerender({ pipelines });
        });

        expect(useAppStore.getState().selectedPipeline?.name).toBe('acme-daily');
    });
});

describe('useStoreInit › navigateToExecution', () => {
    it('returns a navigateToExecution function that calls router.push to /pipelines/', () => {
        const pipelines = [
            { name: 'acme-daily', display_name: 'ACME Daily' },
        ] as any[];

        const { result } = renderHook(() => useStoreInit({ pipelines }));

        act(() => {
            result.current.navigateToExecution('acme-daily', 'exec-123', '2026-05-22');
        });

        expect(mockRouterPush).toHaveBeenCalledWith('/pipelines/');
        expect(useAppStore.getState().date).toBe('2026-05-22');
    });

    it('selects the target execution when navigating within the SAME pipeline and no targetDate is given (matches the notification-click call shape: navigateToExecution(name, execId))', () => {
        const pipelines = [
            { name: 'acme-daily', display_name: 'ACME Daily' },
        ] as any[];

        // Pre-set: already viewing acme-daily on some date, no execution selected.
        act(() => {
            useAppStore.getState().setSelectedPipeline(pipelines[0]);
            useAppStore.getState().setDate('2026-05-22');
            useAppStore.getState().setSelectedExecution(null);
        });

        const { result, rerender } = renderHook(() => useStoreInit({ pipelines }));

        act(() => {
            // Exactly App.tsx's onNotificationNavigate shape: no third arg.
            result.current.navigateToExecution('acme-daily', 'exec-456');
        });
        rerender();

        expect(useAppStore.getState().selectedExecution?.execution_id).toBe('exec-456');
    });

    it('selects the target execution directly when pipeline AND explicit date both match the current state', () => {
        const pipelines = [
            { name: 'acme-daily', display_name: 'ACME Daily' },
        ] as any[];

        act(() => {
            useAppStore.getState().setSelectedPipeline(pipelines[0]);
            useAppStore.getState().setDate('2026-05-22');
            useAppStore.getState().setSelectedExecution(null);
        });

        const { result, rerender } = renderHook(() => useStoreInit({ pipelines }));

        act(() => {
            result.current.navigateToExecution('acme-daily', 'exec-789', '2026-05-22');
        });
        rerender();

        expect(useAppStore.getState().selectedExecution?.execution_id).toBe('exec-789');
    });
});
