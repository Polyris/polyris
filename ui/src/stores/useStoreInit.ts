/**
 * useStoreInit — Initialize and sync Zustand store with URL deep state and side effects.
 *
 * Must be called once in App.tsx. Handles:
 * - URL → store on mount (deep state: pipeline, mode, date, execution)
 * - store → URL on changes (deep state via window.history, silent)
 * - Pipeline restoration from localStorage
 * - Execution/task clearing on switches
 * - Theme application to DOM
 *
 * Top-level view ('pipelines' | 'assets' | 'tasks' | 'runs') lives in URL pathname
 * via Next.js file-system routes, NOT in Zustand. CloudFront Function rewrites
 * `/pipelines/anything` → `/pipelines/index.html` so S3 finds the file regardless
 * of the deep path.
 */

import { useEffect, useRef, useState } from 'react';
import { useClientRoute } from '@/hooks/useClientRoute';
import { useUrlSync } from '../hooks/useUrlSync';
import { useAppStore } from './useAppStore';
import { toDateString } from '../utils';
import type { PipelineWithUI } from '../types';
import { isViewMode } from '../types';

interface UseStoreInitOptions {
    pipelines: PipelineWithUI[];
}

/** Pathname matches /pipelines or /pipelines/ */
function isPipelinesRoute(pathname: string | null): boolean {
    if (!pathname) return false;
    const normalized = pathname.replace(/\/+$/, '');
    return normalized === '/pipelines' || normalized === '';
}

interface PipelinesUrlState {
    pipeline?: string;
    mode?: string;
    date?: string;
    execution?: string;
    [key: string]: string | undefined;
}

const PIPELINES_URL_KEYS = ['pipeline', 'mode', 'date', 'execution'] as const;
const PIPELINES_URL_DEFAULTS = { mode: 'dag' };

export function useStoreInit({ pipelines }: UseStoreInitOptions) {
    const { pathname, push } = useClientRoute();
    const onPipelinesRoute = isPipelinesRoute(pathname);
    const store = useAppStore();
    
    // ========== URL Sync (deep state for /pipelines route only) ==========
    const { initialState: urlState, updateUrl, replaceUrl } = useUrlSync<PipelinesUrlState>({
        keys: PIPELINES_URL_KEYS,
        defaults: PIPELINES_URL_DEFAULTS,
        onChange: (state) => {
            // Browser back/forward: apply URL → store, but only on /pipelines
            if (!onPipelinesRoute) return;
            if (state.mode && isViewMode(state.mode)) store.setViewMode(state.mode);
            if (state.date) store.setDate(state.date);
            if (state.pipeline) {
                urlPipelineRef.current = state.pipeline;
            } else {
                store.setSelectedPipeline(null);
            }
        },
    });

    // Track URL pipeline name for restoration after pipelines load
    const urlPipelineRef = useRef<string | null>(urlState.pipeline || null);

    // Initialize date/mode from URL or today (mount-once).
    //
    // CRITICAL: this MUST be a state flag (not a ref), and push/replace
    // effects below MUST gate on it. Reason: on first commit, all effects
    // fire in declaration order. The mount-once effect schedules
    // store.setDate(urlState.date); the push effect runs immediately
    // after, but its closure still holds the STALE store.date snapshot
    // (today) because Zustand state updates don't propagate mid-commit.
    // Without gating, the push effect rebuilds the URL with the stale
    // date and STRIPS the date param via pushState. After re-render
    // applies the new store.date, the replace effect re-adds it, but
    // the URL has already flickered, and any code that reads URL
    // synchronously during the gap sees no date.
    //
    // Real-world symptom (ADR #63 partition click, fixed v0.78.7):
    // /backfills/bf-X → click partition for 2026-05-22 → router.push
    // /pipelines/?pipeline=A&date=2026-05-22. Without gating: URL
    // briefly flickers to /pipelines/?pipeline=A (no date) before
    // settling. Pipeline detail mounts during the gap, reads
    // store.date = today, fetches today's executions. User reports
    // "redirected to today instead of the partition's date".
    const [isInitialized, setIsInitialized] = useState(false);
    useEffect(() => {
        if (isInitialized) return;
        // Read URL fresh here in case useUrlSync.initialState was computed
        // before the URL was final (SSG hydration edge case).
        const params = new URLSearchParams(
            typeof window !== 'undefined' ? window.location.search : ''
        );
        const urlDate = params.get('date') || urlState.date;
        const urlMode = params.get('mode') || urlState.mode;
        store.setDate(urlDate || toDateString(new Date()));
        if (urlMode && isViewMode(urlMode)) store.setViewMode(urlMode);
        // eslint-disable-next-line react-hooks/set-state-in-effect -- mount-once initialization from URL state
        setIsInitialized(true);
    // Mount-only: initialize store from URL state once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // ========== URL Sync Effects (only on /pipelines, AFTER initialization) ==========

    // Push on pipeline change
    useEffect(() => {
        if (!onPipelinesRoute) return;
        if (!isInitialized) return;
        updateUrl({
            pipeline: store.selectedPipeline?.name || undefined,
            mode: store.viewMode,
            date: store.date !== toDateString(new Date()) ? store.date : undefined,
        });
    // Push URL on pipeline changes; gated by pathname AND initialization
    // to prevent stripping the URL before mount-once has applied URL→store.
    // Omits: updateUrl (stable ref), store.viewMode/date (handled by replaceUrl below)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [store.selectedPipeline?.name, onPipelinesRoute, isInitialized]);

    // Replace on mode/date change
    useEffect(() => {
        if (!onPipelinesRoute) return;
        if (!isInitialized) return;
        replaceUrl({
            pipeline: store.selectedPipeline?.name || undefined,
            mode: store.viewMode,
            date: store.date !== toDateString(new Date()) ? store.date : undefined,
        });
    // Omits: replaceUrl (stable ref), store.selectedPipeline (handled by push above)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [store.viewMode, store.date, onPipelinesRoute, isInitialized]);

    // ========== Pipeline Restoration ==========

    useEffect(() => {
        if (pipelines.length > 0 && !store.selectedPipeline) {
            const urlName = urlPipelineRef.current;
            const lastName = readLastPipeline();
            const targetName = urlName || lastName;
            if (targetName) {
                const target = pipelines.find(p => p.name === targetName);
                if (target) store.setSelectedPipeline(target);
                if (urlName) urlPipelineRef.current = null;
            }
        }
    // Omits: store.setSelectedPipeline (stable Zustand setter, identity never changes)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [pipelines, store.selectedPipeline]);

    // ========== Theme ==========

    useEffect(() => {
        document.documentElement.setAttribute('data-theme', store.theme);
    }, [store.theme]);

    // ========== Execution/Task Clearing ==========

    // Clear task when execution changes
    useEffect(() => {
        if (store.selectedExecution?.execution_id) {
            store.setSelectedTaskName(null);
        }
    // Omits: store.setSelectedTaskName (stable Zustand setter)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [store.selectedExecution?.execution_id]);

    // Pipeline/date switching — reset execution and task
    const prevPipelineRef = useRef<string | null>(null);
    const prevDateRef = useRef(store.date);
    const pendingNavigationRef = useRef<{ execution_id: string; date?: string } | null>(null);

    useEffect(() => {
        const prevName = prevPipelineRef.current;
        const currName = store.selectedPipeline?.name;
        const dateChanged = prevDateRef.current !== store.date;

        prevPipelineRef.current = currName ?? null;
        prevDateRef.current = store.date;

        if (!currName) return;

        const pending = pendingNavigationRef.current;
        pendingNavigationRef.current = null;

        if (prevName !== currName || dateChanged || pending) {
            store.setSelectedTaskName(null);
            if (pending?.execution_id) {
                store.setSelectedExecution({ ...pending, auto_selected: true });
            } else if (prevName !== currName || dateChanged) {
                store.setSelectedExecution(null);
            }
        }
    // Omits: store.setSelectedTaskName, store.setSelectedExecution (stable Zustand setters)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [store.selectedPipeline?.name, store.date]);

    // ========== Helpers ==========

    /** Navigate to a pipeline+execution from AllRuns/Notifications.
     *  Sets store state and routes to /pipelines/. */
    const navigateToExecution = (targetPipelineName: string, executionId?: string, targetDate?: string) => {
        const pipeline = pipelines.find(p => p.name === targetPipelineName);
        if (!pipeline) return;

        const samePipeline = store.selectedPipeline?.name === targetPipelineName;
        const sameDate = !targetDate || store.date === targetDate;

        if (targetDate) store.setDate(targetDate);

        if (samePipeline && sameDate && executionId) {
            store.setSelectedExecution({ execution_id: executionId, auto_selected: false });
        } else if (executionId) {
            pendingNavigationRef.current = { execution_id: executionId, date: targetDate };
        }

        store.setSelectedPipeline(pipeline);
        push('/pipelines/');
    };

    return {
        navigateToExecution,
    };
}

function readLastPipeline(): string | null {
    try { return localStorage.getItem('lastPipeline'); } catch { return null; }
}
