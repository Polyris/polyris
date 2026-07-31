import React, { useState, useMemo, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { TableSkeleton } from './Skeletons';
import { formatDuration, formatApiErrorMessage } from '../utils';
import { Search, Inbox, AlertTriangle } from '../utils/icons';
import { EmptyState } from './EmptyState';
import { WorkspaceFilterChips, type WorkspaceFilterChip } from './WorkspaceMetrics';
import { HistoryChrome } from './HistoryChrome';
import { DatePicker } from './DatePicker';
import { SortableHeader } from './SortableHeader';
import { useAppStore } from '@/stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { useAllTasksQuery, usePipelinesQuery } from '@/hooks/queries';
import { useUrlSync } from '@/hooks/useUrlSync';
import { usePagedRows } from '@/hooks/usePagedRows';
import { useKeyboardShortcuts, SHORTCUTS } from '@/hooks';
import type { PipelineWithUI, Task } from '@/types';

interface AllTasksViewProps {
    onPipelineClick: (pipeline: PipelineWithUI, date?: string) => void;
}

interface TasksUrlState {
    status?: string;
    date?: string;
    pipeline?: string;
    taskName?: string;
    [key: string]: string | undefined;
}
const TASKS_URL_KEYS = ['status', 'date', 'pipeline', 'taskName'] as const;

/**
 * AllTasksView — the Tasks half of the History view (merged with All runs via HistoryChrome).
 */
export function AllTasksView({
    onPipelineClick,
}: AllTasksViewProps) {
    const { taskFilter: filter, setTaskFilter: onFilterChange } = useAppStore(useShallow(s => ({
        taskFilter: s.taskFilter, setTaskFilter: s.setTaskFilter,
    })));

    // ─── URL Sync ─────────────────────────────────────────────────────────
    const { initialState: urlInitial, replaceUrl: replaceTasksUrl } = useUrlSync<TasksUrlState>({
        keys: TASKS_URL_KEYS,
        onChange: (state) => {
            // Browser back/forward — apply URL → filter
            onFilterChange({
                status: state.status || '',
                date: state.date || '',
                pipeline: state.pipeline || '',
                taskName: state.taskName || '',
            });
        },
    });

    // On mount, apply URL state to filter (if any params present)
    const initialized = useRef(false);
    useEffect(() => {
        if (initialized.current) return;
        initialized.current = true;
        if (urlInitial.status || urlInitial.date || urlInitial.pipeline || urlInitial.taskName) {
            onFilterChange({
                status: urlInitial.status || '',
                date: urlInitial.date || '',
                pipeline: urlInitial.pipeline || '',
                taskName: urlInitial.taskName || '',
            });
        }
    // Mount-only: initialize once from URL state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    // Mirror filter → URL silently
    useEffect(() => {
        replaceTasksUrl({
            status: filter.status || undefined,
            date: filter.date || undefined,
            pipeline: filter.pipeline || undefined,
            taskName: filter.taskName || undefined,
        });
    // Omits replaceTasksUrl (stable callback ref)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filter.status, filter.date, filter.pipeline, filter.taskName]);

    const { data: pipelines = [] } = usePipelinesQuery();
    const allTasksQuery = useAllTasksQuery(filter, true);
    const {
        data: taskPages, isLoading: loading, isFetching, isError, error,
        fetchNextPage, hasNextPage, isFetchingNextPage,
    } = allTasksQuery;
    // Every page loaded so far, newest first — the API already ordered them.
    const tasks = useMemo(
        () => (taskPages?.pages ?? []).flatMap((p: { tasks: Task[] }) => p.tasks),
        [taskPages],
    );
    const [sort, setSort] = useState({ key: '', dir: 'desc' });
    const [search, setSearch] = useState('');
    const searchInputRef = useRef<HTMLInputElement>(null);

    // List-view shortcuts (ADR #64): refresh + focus search.
    useKeyboardShortcuts({
        [SHORTCUTS.REFRESH]: () => allTasksQuery.refetch(),
        [SHORTCUTS.FOCUS_FILTER]: () => searchInputRef.current?.focus(),
    });

    const handleSort = (key: string) => {
        setSort(prev => ({
            key,
            dir: prev.key === key && prev.dir === 'desc' ? 'asc' : 'desc'
        }));
    };

    const sortedTasks = useMemo(() => {
        if (!sort.key) return tasks;
        const sorted = [...tasks].sort((a, b) => {
            let va: string | number = '', vb: string | number = '';
            switch (sort.key) {
                case 'task_name': va = a.task_name || ''; vb = b.task_name || ''; break;
                case 'pipeline_name': va = a.pipeline_name || ''; vb = b.pipeline_name || ''; break;
                case 'status': va = a.status || ''; vb = b.status || ''; break;
                case 'date': va = a.date || ''; vb = b.date || ''; break;
                case 'duration_ms': va = a.duration_ms || 0; vb = b.duration_ms || 0; break;
                case 'started_at': va = a.running_at || a.started_at || ''; vb = b.running_at || b.started_at || ''; break;
                default: return 0;
            }
            if (typeof va === 'number' && typeof vb === 'number') return sort.dir === 'asc' ? va - vb : vb - va;
            return sort.dir === 'asc' ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
        });
        return sorted;
    }, [tasks, sort]);

    const { paged, page, setPage, total, pageCount, pageSize } = usePagedRows(
        sortedTasks,
        search,
        t => `${t.task_name ?? ''} ${t.pipeline_name ?? ''}`,
    );

    const hasActiveFilters = !!(filter.status || filter.date || filter.pipeline || filter.taskName);

    const clearFilters = () => {
        onFilterChange({ status: '', date: '', pipeline: '', taskName: '' });
    };

    const filterChips: WorkspaceFilterChip[] = [];
    if (filter.pipeline) filterChips.push({ label: 'Pipeline', value: filter.pipeline, onRemove: () => onFilterChange({ ...filter, pipeline: '' }) });
    if (filter.status) filterChips.push({ label: 'Status', value: filter.status, onRemove: () => onFilterChange({ ...filter, status: '' }) });
    if (filter.taskName) filterChips.push({ label: 'Task', value: filter.taskName, onRemove: () => onFilterChange({ ...filter, taskName: '' }) });
    if (filter.date) filterChips.push({ label: 'Date', value: filter.date, onRemove: () => onFilterChange({ ...filter, date: '' }) });

    const handlePipelineClick = (pipelineName: string, taskDate: string) => {
        const pipeline = pipelines.find(p => p.name === pipelineName);
        if (pipeline) {
            onPipelineClick(pipeline, taskDate);
        }
    };

    const filters = (
        <>
            <select
                value={filter.pipeline}
                onChange={e => onFilterChange({ ...filter, pipeline: e.target.value })}
                className="input-sm"
                aria-label="Filter by pipeline"
            >
                <option value="">All Pipelines</option>
                {pipelines.map(p => (
                    <option key={p.name} value={p.name}>{p.name}</option>
                ))}
            </select>

            <select
                value={filter.status}
                onChange={e => onFilterChange({ ...filter, status: e.target.value })}
                className="input-sm"
                aria-label="Filter by status"
            >
                <option value="">All Statuses</option>
                {/* Mirrors the common TaskStatus values (generated/enums.ts) — keep in sync. */}
                <option value="running">Running</option>
                <option value="waiting">Waiting</option>
                <option value="pending">Pending</option>
                <option value="success">Success</option>
                <option value="failed">Failed</option>
                <option value="upstream_failed">Upstream failed</option>
                <option value="aborted">Aborted</option>
                <option value="stopped">Stopped</option>
                <option value="skipped">Skipped</option>
            </select>

            <DatePicker
                value={filter.date}
                onChange={d => onFilterChange({ ...filter, date: d })}
                placeholder="All dates"
                ariaLabel="Filter by date"
            />

            {hasActiveFilters && (
                <Button size="sm" variant="secondary" onClick={clearFilters}>
                    Clear
                </Button>
            )}
        </>
    );

    return (
        <HistoryChrome
            mode="tasks"
            search={search}
            onSearch={setSearch}
            page={page}
            setPage={setPage}
            total={total}
            pageCount={pageCount}
            pageSize={pageSize}
            onRefresh={() => allTasksQuery.refetch()}
            loading={loading}
            isFetching={isFetching}
            hasMore={hasNextPage}
            onLoadMore={() => fetchNextPage()}
            isLoadingMore={isFetchingNextPage}
            filters={filters}
            searchRef={searchInputRef}
        >
            <WorkspaceFilterChips chips={filterChips} />

            <div className="card">
                {loading ? (
                    <TableSkeleton rows={10} cols={6} />
                ) : isError ? (
                    <EmptyState
                        icon={AlertTriangle}
                        tone="error"
                        title="Couldn't load tasks"
                        description={formatApiErrorMessage(error instanceof Error ? error.message : String(error ?? ''))}
                        action={<Button variant="secondary" onClick={() => allTasksQuery.refetch()}>Retry</Button>}
                    />
                ) : total === 0 ? (
                    <EmptyState
                        icon={hasActiveFilters || search ? Search : Inbox}
                        title={hasActiveFilters || search ? 'No matching tasks' : 'No tasks found'}
                        description={hasActiveFilters || search
                            ? 'Try adjusting your filters or search'
                            : 'Task instances will appear here when pipelines run'}
                    />
                ) : (
                    <table className="table-full" aria-label="Task executions">
                        <thead>
                            <tr className="table-header">
                                <SortableHeader label="Task" sortKey="task_name" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Pipeline" sortKey="pipeline_name" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Status" sortKey="status" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Date" sortKey="date" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Duration" sortKey="duration_ms" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Started" sortKey="started_at" currentSort={sort} onSort={handleSort} />
                            </tr>
                        </thead>
                        <tbody>
                            {paged.map((task, i: number) => (
                                <tr key={i} className="border-b">
                                    <td className="p-md">
                                        <span className="text-mono">{task.task_name}</span>
                                    </td>
                                    <td className="p-md">
                                        <span
                                            className="text-accent clickable"
                                            onClick={() => handlePipelineClick(task.pipeline_name, task.date || '')}
                                        >
                                            {task.pipeline_name}
                                        </span>
                                    </td>
                                    <td className="p-md">
                                        <span className={`task-status-badge ${task.status}`}>
                                            {task.status}
                                        </span>
                                    </td>
                                    <td className="table-cell-mono">{task.date}</td>
                                    <td className="table-cell-mono">
                                        {formatDuration(task.duration_ms)}
                                    </td>
                                    <td className="p-md text-sm text-muted">
                                        {(task.running_at || task.started_at) ? new Date(task.running_at || task.started_at || '').toLocaleString() : '-'}
                                    </td>
                                </tr>
                            ))}
                        </tbody>
                    </table>
                )}
            </div>
        </HistoryChrome>
    );
}

export default AllTasksView;
