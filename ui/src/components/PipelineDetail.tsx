import React, { useState, useMemo, useEffect, lazy, Suspense } from 'react';
import { Button } from '@/components/ui/button';
import { DatePicker } from './DatePicker';
import { RefreshButton } from './RefreshButton';
import { 
    DAGGraph, 
    DAGSkeleton, 
    GanttSkeleton,
    ErrorBoundary,
} from './index';
import { POLLING, toDateString } from '../utils';
import { TASK_SUCCESS_STATUSES } from '@/generated/enums';
import { useAppStore } from '../stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { useToast, useKeyboardShortcuts, SHORTCUTS } from '../hooks';
import { useQueryClient } from '@tanstack/react-query';
import { queryKeys } from '@/lib/queryClient';
import { paidSurface } from '@/ee-active.generated';
import { PipelineActionsProvider } from '@/components/PipelineActionsProvider';
import type { PipelineActions, PipelineActionsParams } from '@/types';
import { 
    usePipelineDetailQuery,
    usePipelineExecutionsQuery,
    usePipelineRunsQuery,
    usePipelinesQuery,
    useTaskEventsQuery,
} from '../hooks/queries';
import { 
    GitBranch, 
    BarChart3, 
    Calendar, 
    Play, 
    Pause, 
    Square, 
    Rocket, 
    ActionIcons,
    Clock,
    BookOpen,
    AlertTriangle,
    Inbox,
    ArrowLeft,
    Loader2
} from '../utils/icons';
import type { Task, Execution } from '@/types';

// Lazy load heavy modals (only loaded when opened)
const TaskDetailModal = lazy(() => import('./TaskDetailModal'));

/**
 * PipelineDetail - Main content area showing pipeline execution details.
 * Self-contained: fetches data, manages actions, renders modals.
 */
interface PipelineDetailProps {
    apiError: string | null;
    navigateToExecution: (name: string, execId?: string, date?: string) => void;
}

export function PipelineDetail({ apiError, navigateToExecution }: PipelineDetailProps) {
    const {
        selectedPipeline: pipeline,
        selectedExecution,
        setSelectedExecution,
        viewMode,
        setViewMode,
        date,
        setDate,
        executionPaused, setExecutionPaused,
        liveMode,
        selectedTaskName,
        selectTask,
        showTaskModal, setShowTaskModal,
        openBackfillModal,
        dagViewSource,
        setDagViewSource,
    } = useAppStore(useShallow(s => ({
        selectedPipeline: s.selectedPipeline,
        selectedExecution: s.selectedExecution,
        setSelectedExecution: s.setSelectedExecution,
        viewMode: s.viewMode,
        setViewMode: s.setViewMode,
        date: s.date,
        setDate: s.setDate,
        executionPaused: s.executionPaused, setExecutionPaused: s.setExecutionPaused,
        liveMode: s.liveMode,
        selectedTaskName: s.selectedTaskName,
        selectTask: s.selectTask,
        showTaskModal: s.showTaskModal, setShowTaskModal: s.setShowTaskModal,
        openBackfillModal: s.openBackfillModal,
        dagViewSource: s.dagViewSource,
        setDagViewSource: s.setDagViewSource,
    })));

    // ========== Data ==========
    const [hasActiveCountdown, setHasActiveCountdown] = useState(false);
    const pipelineName = pipeline?.name ?? '';
    // 'current' mode forces no pipeline_execution — the backend's own
    // priority-2 registry fallback (already used by usePipelineTasksList for
    // the backfill picker) returns the currently-deployed structure with no
    // execution tie at all. See SPIKE_CURRENT_STRUCTURE_VS_LATEST_RUN.md.
    const executionId = dagViewSource === 'current' ? null : (selectedExecution?.execution_id ?? null);

    const { 
        data: detailData,
        isLoading,
        isFetching,
    } = usePipelineDetailQuery(pipelineName, date, executionId, {
        refetchInterval: liveMode && pipelineName ? (hasActiveCountdown ? POLLING.ACTIVE : POLLING.IDLE) : false,
    });

    // In 'current' mode, ignore whatever /pipeline-status returned — it falls
    // back to a same-day scan when pipeline_execution is omitted, which can
    // surface an earlier run's real statuses layered onto the just-deployed
    // structure. 'current' means structure only, never execution data.
    const tasks = useMemo(
        () => (dagViewSource === 'current' ? [] : (detailData?.tasks ?? [])),
        [detailData?.tasks, dagViewSource]
    );
    const dag = detailData?.dag ?? null;
    const serverOffsetMs = detailData?.serverOffsetMs ?? 0;

    const { data: executions = [] } = usePipelineExecutionsQuery(pipelineName, date);
    const { data: pipelines = [] } = usePipelinesQuery();

    // ========== Derived ==========
    const selectedTask = useMemo(() => 
        tasks?.find((t: Task) => t.task_name === selectedTaskName) || null,
        [tasks, selectedTaskName]
    );

    const { data: taskEvents = [], isLoading: taskEventsLoading } = useTaskEventsQuery(
        selectedTask?.execution_name ?? null, 
        showTaskModal
    );

    const dagTaskNames = useMemo(() => dag?.nodes?.map(n => n.id) || [], [dag]);
    const filteredTasks = useMemo(() => 
        tasks.filter(t => dagTaskNames.includes(t.task_name)),
        [tasks, dagTaskNames]
    );

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- derive countdown flag from the latest tasks
        setHasActiveCountdown(tasks.some(
            t => ['deps_ready', 'waiting_delay'].includes(t.status) && Number(t.wait_before || 0) > 0
        ));
    }, [tasks]);

    // ========== Auto-select execution ==========
    useEffect(() => {
        if (detailData?.selectedExecution && !selectedExecution) {
            setSelectedExecution(detailData.selectedExecution);
        }
    // Omits: selectedExecution, setSelectedExecution (only auto-select when no execution is chosen)
    }, [detailData?.selectedExecution]); // eslint-disable-line react-hooks/exhaustive-deps

    // ========== Pause detection ==========
    useEffect(() => {
        const hasPausedTasks = filteredTasks.some(t => t.status === 'waiting_paused');
        if (hasPausedTasks && !executionPaused) setExecutionPaused(true);
        else if (!hasPausedTasks && executionPaused) setExecutionPaused(false);
    }, [filteredTasks, executionPaused, setExecutionPaused]);

    // ========== Actions ==========
    const toast = useToast();
    const showToast = (message: string, type = 'info') => toast.show(message, type);

    const queryClient = useQueryClient();
    // eslint-disable-next-line react-hooks/preserve-manual-memoization -- manual useCallback kept intentionally
    const handleRefresh = React.useCallback(() => {
        queryClient.invalidateQueries({ queryKey: queryKeys.pipelines });
        if (pipeline?.name) queryClient.invalidateQueries({ queryKey: ['pipeline', pipeline.name] });
    }, [queryClient, pipeline?.name]);

    // Pipeline action provider — free (ADR #110): task/execution intervention
    // ships in every build, so the provider is always present (no OSS gating).
    const actionParams: PipelineActionsParams = {
        selectedPipeline: pipeline,
        selectedTask,
        selectedExecution,
        executions,
        tasks,
        dag,
        date,
        setDate,
        setSelectedExecution,
        showToast,
        onSelectTask: (task: Task) => selectTask(task?.task_name || null),
    };

    // Team-tier pipeline view-modes (absent in the OSS build) — ADR #99.
    const GanttChart = paidSurface.GanttChart;
    const CalendarView = paidSurface.CalendarView;

    const onTaskSelect = (task: Task) => selectTask(task.task_name);

    // Keyboard shortcuts per ADR #64 (revised v0.78.5).
    //
    // Numeric keys 1-9 are RESERVED for top-level navigation in App.tsx
    // (1=Pipelines, 2=Assets, 3=Tasks, 4=Runs, 5=Backfills). Inner-surface
    // shortcuts use letter keys matching the first letter of the tab name
    // to avoid double-firing handlers and the resulting unpredictable UX.
    useKeyboardShortcuts({
        [SHORTCUTS.REFRESH]: handleRefresh,
        'd': () => setViewMode('dag'),
        'g': () => { if (GanttChart) setViewMode('gantt'); },
        'c': () => { if (CalendarView) setViewMode('calendar'); },
    });

    // ========== UI State ==========
    const [showHistory, setShowHistory] = useState(false);

    const stats = useMemo(() => ({
        done: filteredTasks.filter(t => TASK_SUCCESS_STATUSES.includes(t.status)).length,
        active: filteredTasks.filter(t => t.status === 'running' || t.status === 'deps_ready' || t.status === 'waiting_delay').length,
        failed: filteredTasks.filter(t => t.status === 'failed' || t.status === 'upstream_failed').length,
        wait: filteredTasks.filter(t => t.status === 'waiting').length,
        stopped: filteredTasks.filter(t => t.status === 'stopped' || t.status === 'aborted').length,
        paused: filteredTasks.filter(t => t.status === 'waiting_paused').length,
        total: dagTaskNames.length
    }), [filteredTasks, dagTaskNames.length]);


    // Date of this pipeline's most recent run (from the sidebar stats), so an empty
    // date can offer a one-click jump to the latest execution instead of guessing.
    const latestRunDate = useMemo(() => {
        const full = pipelines.find(p => p.name === pipeline?.name) ?? pipeline;
        return full?.recent_runs?.find(r => r.date)?.date ?? null;
    }, [pipelines, pipeline]);

    // A pipeline that has never run at all defaults to 'current structure'
    // instead of 'run' — there's no execution to show, so 'run' mode would
    // just fall through to the pre-existing "no executions" blueprint
    // fallback with a mismatched toggle state (History side shown as active
    // while the content displayed is actually the blueprint skeleton).
    // Fires once per pipeline (not on every render) — deliberately excludes
    // dagViewSource from its own dependencies so it doesn't override an
    // explicit manual switch back to 'run' afterward. Requires pipelines to
    // have actually loaded (length > 0) — usePipelinesQuery defaults to []
    // while loading, which would otherwise make a not-yet-loaded pipeline
    // look identical to a genuinely never-run one.
    useEffect(() => {
        if (pipelineName && pipelines.length > 0 && latestRunDate === null) {
            setDagViewSource('current');
        }
    }, [pipelineName, pipelines.length, latestRunDate]); // eslint-disable-line react-hooks/exhaustive-deps

    // Close dropdown on outside click
    React.useEffect(() => {
        const handleClick = (e: Event) => {
            if ((e.target as HTMLElement).closest('.pd-dropdown-container')) return;
            setShowHistory(false);
        };
        if (showHistory) {
            document.addEventListener('click', handleClick as EventListener);
            return () => document.removeEventListener('click', handleClick as EventListener);
        }
    }, [showHistory]);

    const content = (actions: PipelineActions | null) => (
        <>
        <section className="pd-canvas">
            {/* Error Banner */}
            {apiError && <div className="pd-error-banner"><AlertTriangle size={16} /> {apiError}</div>}

            {/* Canvas Header */}
            <div className="pd-canvas-header">
                <div>
                    <div className="pd-canvas-title">{pipeline?.name || 'Select a pipeline'}</div>
                    <div className="pd-canvas-subtitle flex items-center gap-md">
                        {pipeline ? (
                            dagViewSource === 'current'
                                ? <span>Current deployed structure</span>
                                : <span>{selectedExecution?.date || date}</span>
                        ) : (
                            'Choose from the sidebar'
                        )}
                    </div>
                </div>

                {/* Actions */}
                {pipeline && (
                    <div className="flex items-center gap-lg">
                        {/* View Mode Tabs — only when more than one view exists.
                            In OSS, DAG is the only view, so no tab strip is shown. */}
                        {(GanttChart || CalendarView) && (
                        <div className="nav-pills" role="tablist" aria-label="View mode">
                            <div 
                                className={`nav-pill nav-pill--md ${viewMode === 'dag' ? 'active' : ''}`} 
                                onClick={() => setViewMode('dag')}
                                role="tab"
                                aria-selected={viewMode === 'dag'}
                                tabIndex={viewMode === 'dag' ? 0 : -1}
                                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setViewMode('dag'); } }}
                            >
                                <GitBranch size={14} /> DAG
                            </div>
                            {GanttChart && (
                            <div 
                                className={`nav-pill nav-pill--md ${viewMode === 'gantt' ? 'active' : ''}`} 
                                onClick={() => setViewMode('gantt')}
                                role="tab"
                                aria-selected={viewMode === 'gantt'}
                                tabIndex={viewMode === 'gantt' ? 0 : -1}
                                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setViewMode('gantt'); } }}
                            >
                                <BarChart3 size={14} /> Gantt
                            </div>
                            )}
                            {CalendarView && (
                            <div 
                                className={`nav-pill nav-pill--md ${viewMode === 'calendar' ? 'active' : ''}`} 
                                onClick={() => setViewMode('calendar')}
                                role="tab"
                                aria-selected={viewMode === 'calendar'}
                                tabIndex={viewMode === 'calendar' ? 0 : -1}
                                onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setViewMode('calendar'); } }}
                            >
                                <Calendar size={14} /> Calendar
                            </div>
                            )}
                        </div>
                        )}

                        {/* Definition + History — two independent buttons with the
                            same spacing pattern as the other action groups. Left:
                            switch to the current deployed definition (blueprint
                            mode, no execution data — see
                            docs/reference/SPIKE_CURRENT_STRUCTURE_VS_LATEST_RUN.md).
                            Right: ExecutionDropdown's own History button — opens
                            the picker, lists every run, and doubles as the "active"
                            indicator for run mode (highlighted whenever
                            dagViewSource is 'run', whether the run was
                            auto-selected or picked from the list). Defaults to
                            'run' always. */}
                        <div className="structure-source-toggle" role="group" aria-label="Definition source">
                            <button
                                type="button"
                                onClick={() => { setDagViewSource('current'); setShowHistory(false); }}
                                aria-pressed={dagViewSource === 'current'}
                                className={`structure-source-toggle-btn ${dagViewSource === 'current' ? 'active' : ''}`}
                                title="Show what's deployed right now, independent of any run"
                            >
                                Definition
                            </button>
                            <ExecutionDropdown
                                pipelineName={pipeline?.name || ''}
                                showHistory={showHistory}
                                onToggleHistory={() => setShowHistory(!showHistory)}
                                onCloseHistory={() => setShowHistory(false)}
                                isActive={dagViewSource === 'run'}
                            />
                        </div>

                        {/* Action buttons — state-driven. Refresh is icon-only utility;
                            the run/pause/resume/stop set switches on pipeline state. */}
                        <div className="pd-canvas-actions">
                            <div className="pd-action-group pd-action-group--utility">
                                <RefreshButton onRefresh={handleRefresh} isFetching={isFetching} iconSize={16} />
                            </div>

                            {actions && (
                                executionPaused ? (
                                    <div className="pd-action-group pd-action-group--danger">
                                        <Button
                                            variant="default"
                                            className="bg-green-600 hover:bg-green-700"
                                            onClick={actions.handlePauseResume}
                                            disabled={!!actions.pendingAction}
                                            title="Resume pipeline execution"
                                        >
                                            {actions.pendingAction === 'resume'
                                                ? <><Loader2 size={14} className="animate-spin" /> Resuming…</>
                                                : <><Play size={14} /> Resume</>}
                                        </Button>
                                        <span className="pd-action-sep" aria-hidden="true" />
                                        <Button variant="destructive" onClick={actions.handleStop} disabled={!!actions.pendingAction}>
                                            {actions.pendingAction === 'stop'
                                                ? <><Loader2 size={14} className="animate-spin" /> Stopping…</>
                                                : <><Square size={14} /> Stop</>}
                                        </Button>
                                    </div>
                                ) : (stats.active > 0 || stats.wait > 0) ? (
                                    <div className="pd-action-group pd-action-group--danger">
                                        <Button
                                            variant="outline"
                                            onClick={actions.handlePauseResume}
                                            disabled={!!actions.pendingAction}
                                            title="Pause pipeline (running tasks will complete)"
                                        >
                                            {actions.pendingAction === 'pause'
                                                ? <><Loader2 size={14} className="animate-spin" /> Pausing…</>
                                                : <><Pause size={14} /> Pause</>}
                                        </Button>
                                        <span className="pd-action-sep" aria-hidden="true" />
                                        <Button variant="destructive" onClick={actions.handleStop} disabled={!!actions.pendingAction}>
                                            {actions.pendingAction === 'stop'
                                                ? <><Loader2 size={14} className="animate-spin" /> Stopping…</>
                                                : <><Square size={14} /> Stop</>}
                                        </Button>
                                    </div>
                                ) : (
                                    <div className="pd-action-group pd-action-group--primary">
                                        <Button onClick={actions.handleRun} disabled={!!actions.pendingAction}>
                                            {actions.pendingAction === 'run'
                                                ? <><Loader2 size={14} className="animate-spin" /> Starting…</>
                                                : <><Rocket size={14} /> Run</>}
                                        </Button>

                                        {paidSurface.BackfillNavTab && (
                                        <Button
                                            variant="secondary"
                                            onClick={() => {
                                                if (!pipeline?.name) return;
                                                openBackfillModal({
                                                    origin: 'pipeline',
                                                    target: { type: 'pipeline', name: pipeline.name },
                                                });
                                            }}
                                            title="Backfill pipeline for date range"
                                        >
                                            <ActionIcons.backfill size={14} /> Backfill
                                        </Button>
                                        )}
                                    </div>
                                )
                            )}
                        </div>
                    </div>
                )}
            </div>

            {/* Paused Execution Banner */}
            {executionPaused && pipeline && actions && (
                <div className="pd-pause-banner">
                    <span className="pd-pause-banner-icon"><Pause size={18} /></span>
                    <span className="pd-pause-banner-text">
                        Pipeline paused – {stats.paused} task{stats.paused !== 1 ? 's' : ''} waiting (12h timeout)
                    </span>
                    <Button size="sm" variant="secondary" onClick={actions.handleExtendPause} title="Extend pause by 12 hours">
                        <Clock size={14} /> +12h
                    </Button>
                    <Button size="sm" className="bg-green-600 hover:bg-green-700" onClick={actions.handlePauseResume}>
                        <Play size={14} /> Resume
                    </Button>
                </div>
            )}

            {/* Main Content */}
            <div className="pd-canvas-content relative">
                {/* Loading Overlay */}
                {isLoading && (
                    <div className="pd-loading-overlay" role="status" aria-live="polite">
                        <div className="pd-loading-spinner"><Loader2 size={24} className="animate-spin" /></div>
                        <span className="sr-only">Loading pipeline data…</span>
                    </div>
                )}

                {/* Content States */}
                {!pipeline ? (
                    <EmptyState
                        icon={<ArrowLeft size={48} className="text-gray-300" />}
                        title="Select a pipeline"
                        text="Choose a pipeline from the sidebar to view its execution graph"
                    />
                ) : isLoading && !dag ? (
                    viewMode === 'gantt' && GanttChart ? <GanttSkeleton count={6} /> : <DAGSkeleton />
                ) : tasks.length === 0 && !isLoading && viewMode !== 'calendar' ? (
                    // Gate on the tasks, because the tasks are what the graph draws. This
                    // asked `executions.length === 0` instead — a different question, to a
                    // different endpoint — so whenever the two disagreed the explanation
                    // silently vanished and left a bare graph with no way out. They agree
                    // now (ADR #106 follow-up), which is exactly why this must not go back
                    // to trusting that they always will.
                    (dag?.nodes?.length ?? 0) > 0 ? (
                        <div className="relative h-full">
                            <DAGGraph
                                dag={dag}
                                tasks={[]}
                                selectedTask={null}
                                onSelectTask={() => {}}
                                serverOffsetMs={0}
                                isBlueprint={true}
                            />
                            <div className="absolute bottom-6 left-1/2 -translate-x-1/2 bg-[var(--bg-primary)] px-5 py-2.5 rounded-lg shadow-lg border border-[var(--border)] z-10 text-[var(--text-muted)] text-[13px] flex items-center gap-3">
                                {dagViewSource === 'current' ? (
                                    <>
                                        <span>Showing the current deployed structure — no execution data</span>
                                        {latestRunDate && (
                                            <Button
                                                size="sm"
                                                onClick={() => { setSelectedExecution(null); setDate(latestRunDate); }}
                                            >
                                                View latest run · {latestRunDate}
                                            </Button>
                                        )}
                                    </>
                                ) : (
                                    <>
                                        <span>No executions for {date}</span>
                                        {latestRunDate && latestRunDate !== date ? (
                                            <Button
                                                size="sm"
                                                onClick={() => { setSelectedExecution(null); setDate(latestRunDate); }}
                                            >
                                                View latest run · {latestRunDate}
                                            </Button>
                                        ) : (
                                            <span>— open <strong>Execution history</strong> to pick another date</span>
                                        )}
                                    </>
                                )}
                            </div>
                        </div>
                    ) : (
                    <EmptyState
                        icon={<Inbox size={48} className="text-gray-300" />}
                        title={`No executions for ${date}`}
                        text="This pipeline has no runs on this date."
                    >
                        {latestRunDate && latestRunDate !== date && (
                            <Button onClick={() => { setSelectedExecution(null); setDate(latestRunDate); }}>
                                View latest run · {latestRunDate}
                            </Button>
                        )}
                    </EmptyState>
                    )
                ) : viewMode === 'calendar' && CalendarView ? (
                    <ErrorBoundary fallback={<ErrorFallback message="Failed to load calendar." onRetry={() => window.location.reload()} />}>
                        <CalendarView
                            executions={executions}
                            selectedDate={date}
                            onSelectDate={(d: string) => {
                                setDate(d);
                                setSelectedExecution(null);
                            }}
                            pipelineName={pipeline.name}
                        />
                    </ErrorBoundary>
                ) : viewMode === 'gantt' && GanttChart ? (
                    <ErrorBoundary fallback={<ErrorFallback message="Failed to load Gantt chart." onRetry={() => setViewMode('dag')} retryText="Switch to DAG" />}>
                        <GanttChart
                            tasks={filteredTasks}
                            selectedTask={selectedTask}
                            onSelectTask={onTaskSelect}
                        />
                    </ErrorBoundary>
                ) : (
                    <ErrorBoundary fallback={<ErrorFallback message="Failed to load DAG." onRetry={() => setViewMode('table')} retryText="Switch to Table" />}>
                        <DAGGraph
                            dag={dag}
                            tasks={filteredTasks}
                            selectedTask={selectedTask}
                            onSelectTask={onTaskSelect}
                            serverOffsetMs={serverOffsetMs}
                        />
                    </ErrorBoundary>
                )}
            </div>
        </section>

        {/* Task Detail Modal */}
        {showTaskModal && selectedTask && (
            <Suspense fallback={null}>
            <TaskDetailModal
                task={selectedTask}
                tasks={tasks}
                dag={dag}
                pipelines={pipelines}
                taskEvents={taskEvents}
                taskEventsLoading={taskEventsLoading}
                onClose={() => setShowTaskModal(false)}
                onAction={actions ? (action: string) => actions.handleTaskAction(action, selectedTask) : undefined}
                onRunAction={actions?.handleRunAction}
                onTaskSelect={(task: Task) => selectTask(task.task_name)}
                onOpenPipeline={(name: string, d?: string | null) => {
                    setShowTaskModal(false);
                    navigateToExecution(name, undefined, d || undefined);
                }}
                onPauseResume={actions?.handlePauseResume}
                serverOffsetMs={serverOffsetMs}
            />
            </Suspense>
        )}
        </>
    );

    return (
        <PipelineActionsProvider params={actionParams}>
            {(actions) => content(actions)}
        </PipelineActionsProvider>
    );
}

/**
 * ExecutionDropdown — reads selectedExecution and date from store
 */
function ExecutionDropdown({
    pipelineName,
    showHistory,
    onToggleHistory,
    onCloseHistory,
    isActive
}: {
    pipelineName: string;
    showHistory: boolean;
    onToggleHistory: () => void;
    onCloseHistory: () => void;
    isActive: boolean;
}) {
    const { selectedExecution, setSelectedExecution, date, setDate } = useAppStore(useShallow(s => ({
        selectedExecution: s.selectedExecution, setSelectedExecution: s.setSelectedExecution,
        date: s.date, setDate: s.setDate,
    })));

    // The picker filters this list; it no longer scopes the page. The page's date
    // follows whichever run you pick, so an empty filter can't strand you on a
    // date with no runs.
    const [filterDate, setFilterDate] = React.useState('');

    // Every run, newest first — no date window. Fetched even while the drawer is
    // shut: the button shows the count, so gating this on `showHistory` would
    // render "0" until you opened it.
    const { data, fetchNextPage, hasNextPage, isFetchingNextPage, isLoading } =
        usePipelineRunsQuery(pipelineName);

    const allRuns = React.useMemo(
        () => (data?.pages ?? []).flatMap((p: { executions: Execution[] }) => p.executions),
        [data],
    );
    const runs = React.useMemo(
        () => (filterDate ? allRuns.filter((r: Execution) => r.date === filterDate) : allRuns),
        [allRuns, filterDate],
    );

    const today = toDateString(new Date());

    return (
        <div className="pd-dropdown-container">
            <button
                type="button"
                onClick={onToggleHistory}
                title="History"
                aria-label={`History (${runs.length})`}
                aria-pressed={isActive}
                className={`structure-source-toggle-btn structure-source-toggle-btn--icon ${isActive ? 'active' : ''}`}
            >
                <BookOpen size={16} />
                <span className="pd-history-count">{runs.length}</span>
            </button>

            {showHistory && (
                <div className="pd-exec-dropdown" role="dialog" aria-label="Execution history">
                    <div className="pd-exec-dropdown-head">
                        <span className="pd-exec-dropdown-title">Execution history</span>
                    </div>

                    {/* Optional filter — empty means every run we can still see */}
                    <div className="pd-exec-dropdown-datebar">
                        <DatePicker
                            value={filterDate}
                            onChange={setFilterDate}
                            placeholder="All dates"
                            ariaLabel="Filter runs by date"
                        />
                        {filterDate && (
                            <Button
                                variant="secondary"
                                size="sm"
                                className="ml-auto"
                                onClick={() => setFilterDate('')}
                                title="Show every run"
                            >
                                Clear
                            </Button>
                        )}
                    </div>

                    <div className="pd-exec-dropdown-list">
                        {isLoading && <div className="pd-exec-dropdown-empty">Loading…</div>}
                        {!isLoading && runs.length === 0 && (
                            <div className="pd-exec-dropdown-empty">
                                {filterDate ? 'No runs on this date' : 'No runs yet'}
                            </div>
                        )}
                            {runs.map((ex: Execution, i: number) => {
                                const isSelected = selectedExecution?.execution_id === ex.execution_id ||
                                                  (!selectedExecution && ex.date === date);
                                const statusClass = ex.status || 'waiting';

                                return (
                                    <div
                                        key={i}
                                        className={`pd-dropdown-item ${isSelected ? 'selected' : ''} ${ex.date === today ? 'today' : ''}`}
                                        onClick={() => {
                                            setSelectedExecution({ ...ex, auto_selected: false });
                                            if (ex.date) setDate(ex.date);
                                            onCloseHistory();
                                        }}
                                    >
                                        <div className="flex-between">
                                            <span className="pd-dropdown-item-title">
                                                {ex.execution_short || (ex.execution_id ? ex.execution_id.substring(0, 8) : ex.date)}...
                                            </span>
                                            <span className={`pd-exec-status-mini ${statusClass}`}>
                                                {ex.status || 'unknown'}
                                            </span>
                                        </div>
                                        <div className="pd-dropdown-item-meta">
                                            {ex.started_at ? new Date(ex.started_at).toLocaleString() : ex.date}
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                    {/* History goes back as far as the rows still exist (row TTL);
                        there is no window to widen — just keep loading. */}
                    {hasNextPage && !filterDate && (
                        <div className="pd-exec-dropdown-more">
                            <Button
                                variant="secondary"
                                size="sm"
                                onClick={() => fetchNextPage()}
                                disabled={isFetchingNextPage}
                            >
                                {isFetchingNextPage ? 'Loading…' : 'Show older runs'}
                            </Button>
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

/**
 * EmptyState - Empty state placeholder
 */
function EmptyState({ icon, title, text, children }: { icon: React.ReactNode; title: string; text?: string; children?: React.ReactNode }) {
    return (
        <div className="empty-state">
            <div className="empty-state-icon">{icon}</div>
            <div className="empty-state-title">{title}</div>
            <div className="empty-state-text">
                {text}
                {children}
            </div>
        </div>
    );
}

/**
 * ErrorFallback - Error fallback UI
 */
function ErrorFallback({ message, onRetry, retryText = 'Reload' }: { message: string; onRetry: () => void; retryText?: string }) {
    return (
        <div className="pd-error-fallback">
            {message} <button onClick={onRetry}>{retryText}</button>
        </div>
    );
}

export default PipelineDetail;
