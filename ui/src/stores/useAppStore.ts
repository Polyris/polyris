/**
 * useAppStore — Zustand store for shared UI and navigation state.
 *
 * Top-level navigation (which page is shown) lives in URL pathname via
 * Next.js file-system routes (`/pipelines`, `/assets`, `/tasks`, `/runs`).
 * Deep state inside each page (selected pipeline, view mode, date,
 * execution) lives here and is mirrored to URL search params via
 * `useUrlSync` (window.history silent updates).
 *
 * Usage:
 *   const date = useAppStore(s => s.date);
 *   const { theme, toggleTheme } = useAppStore(useShallow(s => ({...})));
 */

import { create } from 'zustand';
import type { ViewMode, PipelineWithUI, SelectedExecution, TaskFilter, RunFilter, BackfillModalSeed } from '../types';

interface ModalState {
    showHelpModal: boolean;
    /** Universal backfill modal seed (v0.78+, per ADR #51). When set, modal is open. */
    backfillModalSeed: BackfillModalSeed | null;
    showCommandPalette: boolean;
    showTaskModal: boolean;
}

interface AppState extends ModalState {
    // Navigation
    viewMode: ViewMode;
    date: string;
    selectedPipeline: PipelineWithUI | null;
    selectedExecution: SelectedExecution | null;
    selectedTaskName: string | null;
    // Pipeline detail: show the last run's graph (tied to that run's actual
    // structure/statuses) or the currently-deployed structure (registry,
    // no execution — see docs/reference/SPIKE_CURRENT_STRUCTURE_VS_LATEST_RUN.md).
    // Deliberately separate from `viewMode` (dag/gantt/table/calendar layout).
    dagViewSource: 'run' | 'current';

    // UI
    theme: string;
    liveMode: boolean;
    sidebarOpen: boolean;
    pipelineSearch: string;
    executionPaused: boolean;

    // Filters
    taskFilter: TaskFilter;
    runFilter: RunFilter;

    // Navigation actions
    setViewMode: (mode: ViewMode) => void;
    setDate: (date: string) => void;
    setSelectedPipeline: (pipeline: PipelineWithUI | null) => void;
    setSelectedExecution: (exec: SelectedExecution | null) => void;
    setSelectedTaskName: (name: string | null) => void;
    setDagViewSource: (source: 'run' | 'current') => void;

    // UI actions
    setTheme: (theme: string) => void;
    toggleTheme: () => void;
    setLiveMode: (live: boolean) => void;
    toggleLiveMode: () => void;
    setSidebarOpen: (open: boolean) => void;
    toggleSidebar: () => void;
    setPipelineSearch: (search: string) => void;
    setExecutionPaused: (paused: boolean) => void;

    // Filter actions
    setTaskFilter: (filter: TaskFilter) => void;
    setRunFilter: (filter: RunFilter) => void;

    // Modal actions
    setShowHelpModal: (show: boolean) => void;
    /** Open the universal backfill modal with seed data (v0.78+). */
    openBackfillModal: (seed: BackfillModalSeed) => void;
    /** Close the universal backfill modal. */
    closeBackfillModal: () => void;
    setShowCommandPalette: (show: boolean) => void;
    setShowTaskModal: (show: boolean) => void;

    // Compound actions
    /** Select task and open modal */
    selectTask: (taskName: string | null) => void;
    /** Clear all selection state */
    clearSelection: () => void;
}

/** Read persisted value from localStorage */
function readPersisted<T>(key: string, fallback: T): T {
    if (typeof window === 'undefined') return fallback;
    try {
        const raw = localStorage.getItem(key);
        if (raw === null) return fallback;
        // Boolean special case
        if (fallback === true || fallback === false) return (raw === 'true') as T;
        return raw as T;
    } catch {
        return fallback;
    }
}

/** Write to localStorage (fire and forget) */
function persist(key: string, value: string | boolean) {
    if (typeof window === 'undefined') return;
    try { localStorage.setItem(key, String(value)); } catch { /* ignore */ }
}

export const useAppStore = create<AppState>((set) => ({
    // Navigation — top-level view in URL pathname; deep state below
    viewMode: readPersisted<ViewMode>('viewMode', 'dag'),
    date: '', // Set by useStoreInit from URL or today
    selectedPipeline: null,
    selectedExecution: null,
    selectedTaskName: null,
    dagViewSource: 'run',

    // UI — restored from localStorage
    theme: readPersisted('theme', 'light'),
    liveMode: readPersisted('liveMode', true),
    sidebarOpen: false,
    pipelineSearch: '',
    executionPaused: false,

    // Filters
    taskFilter: { status: '', date: '', pipeline: '', taskName: '' },
    runFilter: { status: '', pipeline: '', date: '' },

    // Modals
    showHelpModal: false,
    backfillModalSeed: null,
    showCommandPalette: false,
    showTaskModal: false,

    // Navigation actions (persist where needed)
    setViewMode: (mode) => set(() => {
        persist('viewMode', mode);
        return { viewMode: mode };
    }),
    setDate: (date) => set({ date }),
    setSelectedPipeline: (pipeline) => set(() => {
        if (pipeline?.name) persist('lastPipeline', pipeline.name);
        // Switching pipelines always leaves 'current structure' mode — it's
        // scoped to whichever pipeline you were looking at, not carried over.
        return { selectedPipeline: pipeline, dagViewSource: 'run' };
    }),
    setSelectedExecution: (exec) => set(() => ({
        selectedExecution: exec,
        // Picking a specific execution (history dropdown, notifications via
        // navigateToExecution) always means "show me that run" — 'current
        // structure' mode would otherwise silently override it by forcing
        // executionId back to null.
        dagViewSource: 'run',
    })),
    setSelectedTaskName: (name) => set({ selectedTaskName: name }),
    setDagViewSource: (source) => set({ dagViewSource: source }),

    // UI actions
    setTheme: (theme) => set(() => {
        persist('theme', theme);
        return { theme };
    }),
    toggleTheme: () => set((s) => {
        const next = s.theme === 'light' ? 'dark' : 'light';
        persist('theme', next);
        return { theme: next };
    }),
    setLiveMode: (live) => set(() => {
        persist('liveMode', live);
        return { liveMode: live };
    }),
    toggleLiveMode: () => set((s) => {
        persist('liveMode', !s.liveMode);
        return { liveMode: !s.liveMode };
    }),
    setSidebarOpen: (open) => set({ sidebarOpen: open }),
    toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
    setPipelineSearch: (search) => set({ pipelineSearch: search }),
    setExecutionPaused: (paused) => set({ executionPaused: paused }),

    // Filter actions
    setTaskFilter: (filter) => set({ taskFilter: filter }),
    setRunFilter: (filter) => set({ runFilter: filter }),

    // Modal actions
    setShowHelpModal: (show) => set({ showHelpModal: show }),
    openBackfillModal: (seed) => set({ backfillModalSeed: { ...seed, isOpen: true } }),
    closeBackfillModal: () => set({ backfillModalSeed: null }),
    setShowCommandPalette: (show) => set({ showCommandPalette: show }),
    setShowTaskModal: (show) => set({ showTaskModal: show }),

    // Compound actions
    selectTask: (taskName) => set((state) => ({
        selectedTaskName: taskName,
        showTaskModal: taskName !== null ? true : state.showTaskModal,
    })),
    clearSelection: () => set({
        selectedPipeline: null,
        selectedExecution: null,
        selectedTaskName: null,
        dagViewSource: 'run',
    }),
}));
