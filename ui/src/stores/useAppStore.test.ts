import { describe, it, expect, beforeEach } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useAppStore } from './useAppStore';
import type { PipelineWithUI, SelectedExecution } from '../types';

const makePipeline = (name: string): PipelineWithUI => ({
    name,
    schedule: '0 8 * * *',
    task_count: 3,
    status: 'idle',
}) as PipelineWithUI;

describe('useAppStore', () => {
    beforeEach(() => {
        // Reset store between tests
        const { result } = renderHook(() => useAppStore());
        act(() => {
            result.current.setViewMode('dag');
            result.current.setSelectedPipeline(null);
            result.current.setSelectedExecution(null);
            result.current.setSelectedTaskName(null);
            result.current.setSidebarOpen(false);
            result.current.setShowHelpModal(false);
            result.current.setShowCommandPalette(false);
            result.current.setShowTaskModal(false);
            result.current.closeBackfillModal();
            result.current.setExecutionPaused(false);
            result.current.setTaskFilter({ status: '', date: '', pipeline: '', taskName: '' });
            result.current.setRunFilter({ status: '', pipeline: '' });
        });
        localStorage.clear();
    });

    describe('initial state', () => {
        it('has correct defaults', () => {
            const { result } = renderHook(() => useAppStore());
            expect(result.current.viewMode).toBe('dag');
            expect(result.current.selectedPipeline).toBeNull();
            expect(result.current.selectedExecution).toBeNull();
            expect(result.current.selectedTaskName).toBeNull();
            expect(result.current.sidebarOpen).toBe(false);
            expect(result.current.executionPaused).toBe(false);
            expect(result.current.dagViewSource).toBe('run');
        });
    });

    describe('navigation', () => {
        it('setViewMode updates and persists', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setViewMode('gantt'));
            expect(result.current.viewMode).toBe('gantt');
            expect(localStorage.getItem('viewMode')).toBe('gantt');
        });

        it('setDate updates state', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setDate('2025-06-15'));
            expect(result.current.date).toBe('2025-06-15');
        });

        it('setSelectedPipeline persists name to lastPipeline', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setSelectedPipeline(makePipeline('etl_main')));
            expect(result.current.selectedPipeline?.name).toBe('etl_main');
            expect(localStorage.getItem('lastPipeline')).toBe('etl_main');
        });

        it('setDagViewSource switches between run and current', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setDagViewSource('current'));
            expect(result.current.dagViewSource).toBe('current');
            act(() => result.current.setDagViewSource('run'));
            expect(result.current.dagViewSource).toBe('run');
        });

        it('clearSelection resets pipeline, execution, task, and dagViewSource', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => {
                result.current.setSelectedPipeline(makePipeline('etl'));
                result.current.setSelectedExecution({ execution_id: 'e1', auto_selected: false });
                result.current.setSelectedTaskName('task_a');
                result.current.setDagViewSource('current');
            });
            act(() => result.current.clearSelection());
            expect(result.current.selectedPipeline).toBeNull();
            expect(result.current.selectedExecution).toBeNull();
            expect(result.current.selectedTaskName).toBeNull();
            expect(result.current.dagViewSource).toBe('run');
        });
    });

    describe('execution', () => {
        it('sets and clears execution', () => {
            const { result } = renderHook(() => useAppStore());
            const exec: SelectedExecution = { execution_id: 'exec-1', auto_selected: false };
            act(() => result.current.setSelectedExecution(exec));
            expect(result.current.selectedExecution?.execution_id).toBe('exec-1');

            act(() => result.current.setSelectedExecution(null));
            expect(result.current.selectedExecution).toBeNull();
        });
    });

    describe('theme', () => {
        it('toggleTheme switches between light and dark', () => {
            const { result } = renderHook(() => useAppStore());
            expect(result.current.theme).toBe('light');

            act(() => result.current.toggleTheme());
            expect(result.current.theme).toBe('dark');
            expect(localStorage.getItem('theme')).toBe('dark');

            act(() => result.current.toggleTheme());
            expect(result.current.theme).toBe('light');
        });
    });

    describe('liveMode', () => {
        it('toggleLiveMode switches', () => {
            const { result } = renderHook(() => useAppStore());
            expect(result.current.liveMode).toBe(true);

            act(() => result.current.toggleLiveMode());
            expect(result.current.liveMode).toBe(false);
            expect(localStorage.getItem('liveMode')).toBe('false');
        });
    });

    describe('sidebar', () => {
        it('toggleSidebar switches', () => {
            const { result } = renderHook(() => useAppStore());
            expect(result.current.sidebarOpen).toBe(false);
            act(() => result.current.toggleSidebar());
            expect(result.current.sidebarOpen).toBe(true);
        });
    });

    describe('modals', () => {
        it('sets help modal', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setShowHelpModal(true));
            expect(result.current.showHelpModal).toBe(true);
        });

        it('opens and closes backfill modal via seed', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.openBackfillModal({
                origin: 'pipeline',
                target: { type: 'pipeline', name: 'p1' },
            }));
            expect(result.current.backfillModalSeed).not.toBeNull();
            expect(result.current.backfillModalSeed?.isOpen).toBe(true);
            act(() => result.current.closeBackfillModal());
            expect(result.current.backfillModalSeed).toBeNull();
        });

        it('sets command palette', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setShowCommandPalette(true));
            expect(result.current.showCommandPalette).toBe(true);
        });

        it('sets task modal', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setShowTaskModal(true));
            expect(result.current.showTaskModal).toBe(true);
        });
    });

    describe('filters', () => {
        it('sets task filter', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setTaskFilter({ status: 'failed', date: '', pipeline: 'etl', taskName: '' }));
            expect(result.current.taskFilter.status).toBe('failed');
            expect(result.current.taskFilter.pipeline).toBe('etl');
        });

        it('sets run filter', () => {
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setRunFilter({ status: 'running', pipeline: '', date: '2024-01-15' }));
            expect(result.current.runFilter.status).toBe('running');
        });

        it('run filter owns its own date, separate from the pipeline page scope', () => {
            // Two ideas, two values (ADR #106): the feed's '' means "every date", the
            // page's date scopes a page. Sharing one value stranded the pipeline page
            // on a day called '' whenever this filter was cleared.
            const { result } = renderHook(() => useAppStore());
            act(() => result.current.setDate('2024-01-15'));
            act(() => result.current.setRunFilter({ status: '', pipeline: '', date: '' }));

            expect(result.current.runFilter.date).toBe('');
            expect(result.current.date).toBe('2024-01-15');
        });
    });

    describe('shared state across hooks', () => {
        it('store is global — changes visible to all consumers', () => {
            const { result: r1 } = renderHook(() => useAppStore());
            const { result: r2 } = renderHook(() => useAppStore());

            act(() => r1.current.setViewMode('gantt'));
            expect(r2.current.viewMode).toBe('gantt');

            act(() => r2.current.setSelectedPipeline(makePipeline('shared')));
            expect(r1.current.selectedPipeline?.name).toBe('shared');
        });
    });
});
