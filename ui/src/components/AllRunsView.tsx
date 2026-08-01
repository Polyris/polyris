import React, { useState, useMemo, useEffect, useRef } from 'react';
import { useClientRoute } from '@/hooks/useClientRoute';
import { Button } from '@/components/ui/button';
import { TableSkeleton } from './Skeletons';
import { formatDuration, formatApiErrorMessage } from '../utils';
import {
    Search,
    Inbox,
    AlertTriangle,
    ActionIcons,
} from '../utils/icons';
import { EmptyState } from './EmptyState';
import { WorkspaceFilterChips, type WorkspaceFilterChip } from './WorkspaceMetrics';
import { HistoryChrome } from './HistoryChrome';
import { DatePicker } from './DatePicker';
import { SortableHeader } from './SortableHeader';
import { useAppStore } from '@/stores/useAppStore';
import { useShallow } from 'zustand/react/shallow';
import { useAllRunsQuery, usePipelinesQuery } from '@/hooks/queries';
import { useUrlSync } from '@/hooks/useUrlSync';
import { usePagedRows } from '@/hooks/usePagedRows';
import { useKeyboardShortcuts, SHORTCUTS } from '@/hooks';
import { paidSurface } from '@/ee-active.generated';
import type { PipelineWithUI, Execution, RunFeedRow } from '@/types';

interface AllRunsViewProps {
    onPipelineClick: (pipeline: PipelineWithUI, run: Execution) => void;
}

interface RunsUrlState {
    status?: string;
    pipeline?: string;
    date?: string;
    [key: string]: string | undefined;
}
const RUNS_URL_KEYS = ['status', 'pipeline', 'date'] as const;

/**
 * AllRunsView — the Runs half of the History view (merged with All tasks via HistoryChrome).
 * Backfill rows/column/statuses are a Team surface, so they render only when the paid surface
 * provides the Backfills view.
 */
export function AllRunsView({
    onPipelineClick,
}: AllRunsViewProps) {
    const router = useClientRoute();
    const { runFilter: filter, setRunFilter: onFilterChange } = useAppStore(useShallow(s => ({
        runFilter: s.runFilter, setRunFilter: s.setRunFilter,
    })));

    const showBackfills = !!paidSurface.BackfillsView;

    // ─── URL Sync ─────────────────────────────────────────────────────────
    // The date is this workspace's own filter and lives in this workspace's URL
    // (ADR #106 — a date scopes the data it sits with). It used to be the global
    // `store.date`, which is the *Pipeline page's* scope: clearing the filter here
    // meant "all dates" to this feed and "a day called ''" to that page, which
    // stranded it on an empty graph.
    const { initialState: urlInitial, replaceUrl: replaceRunsUrl } = useUrlSync<RunsUrlState>({
        keys: RUNS_URL_KEYS,
        onChange: (state) => {
            onFilterChange({
                status: state.status || '',
                pipeline: state.pipeline || '',
                date: state.date || '',
            });
        },
    });

    const initialized = useRef(false);
    useEffect(() => {
        if (initialized.current) return;
        initialized.current = true;
        if (urlInitial.status || urlInitial.pipeline || urlInitial.date) {
            onFilterChange({
                status: urlInitial.status || '',
                pipeline: urlInitial.pipeline || '',
                date: urlInitial.date || '',
            });
        }
    // Mount-only: initialize once from URL state.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    useEffect(() => {
        replaceRunsUrl({
            status: filter.status || undefined,
            pipeline: filter.pipeline || undefined,
            date: filter.date || undefined,
        });
    // Omits replaceRunsUrl (stable callback ref)
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [filter.status, filter.pipeline, filter.date]);

    const { data: pipelines = [] } = usePipelinesQuery();
    const {
        data: runsPages, isLoading: loading, isFetching, isError, error, refetch: refetchAllRuns,
        fetchNextPage, hasNextPage, isFetchingNextPage,
    } = useAllRunsQuery(filter.date, filter, true);
    // Every page loaded so far, newest first — the API already ordered them.
    const runs = useMemo(
        () => (runsPages?.pages ?? []).flatMap((p: { runs: RunFeedRow[] }) => p.runs),
        [runsPages],
    );
    const [sort, setSort] = useState({ key: '', dir: 'desc' });
    const [search, setSearch] = useState('');

    // List-view shortcuts (ADR #64): refresh.
    useKeyboardShortcuts({
        [SHORTCUTS.REFRESH]: () => refetchAllRuns(),
    });

    const handleSort = (key: string) => {
        setSort(prev => ({
            key,
            dir: prev.key === key && prev.dir === 'desc' ? 'asc' : 'desc'
        }));
    };

    const sortedRuns = useMemo(() => {
        if (!sort.key) return runs;
        const sorted = [...runs].sort((a, b) => {
            let va: string | number = '', vb: string | number = '';
            switch (sort.key) {
                case 'pipeline_name': va = a.pipeline_name || ''; vb = b.pipeline_name || ''; break;
                case 'status': va = a.status || ''; vb = b.status || ''; break;
                case 'date': va = a.date || ''; vb = b.date || ''; break;
                case 'duration_ms': va = a.duration_ms || 0; vb = b.duration_ms || 0; break;
                case 'started_at': va = a.started_at || ''; vb = b.started_at || ''; break;
                default: return 0;
            }
            if (typeof va === 'number' && typeof vb === 'number') return sort.dir === 'asc' ? va - vb : vb - va;
            return sort.dir === 'asc' ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
        });
        return sorted;
    }, [runs, sort]);

    const { paged, page, setPage, total, pageCount, pageSize } = usePagedRows(
        sortedRuns,
        search,
        r => `${r.pipeline_name ?? ''} ${r.pipeline_execution ?? ''} ${r.backfill_id ?? ''}`,
    );

    const hasActiveFilters = !!(filter.pipeline || filter.status || filter.date);

    const clearFilters = () => {
        onFilterChange({ status: '', pipeline: '', date: '' });
    };

    const filterChips: WorkspaceFilterChip[] = [];
    if (filter.pipeline) filterChips.push({ label: 'Pipeline', value: filter.pipeline, onRemove: () => onFilterChange({ ...filter, pipeline: '' }) });
    if (filter.status) filterChips.push({ label: 'Status', value: filter.status, onRemove: () => onFilterChange({ ...filter, status: '' }) });
    if (filter.date) filterChips.push({ label: 'Date', value: filter.date, onRemove: () => onFilterChange({ ...filter, date: '' }) });

    const handlePipelineClick = (run: RunFeedRow) => {
        const pipeline = pipelines.find(p => p.name === run.pipeline_name);
        if (pipeline) {
            onPipelineClick(pipeline, run);
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
                {/* Mirrors ExecutionStatus (generated/enums.ts, ADR #112) — keep in sync. */}
                <optgroup label="Runs">
                    <option value="running">Running</option>
                    <option value="success">Succeeded</option>
                    <option value="failed">Failed</option>
                    <option value="timed_out">Timed out</option>
                    <option value="aborted">Aborted</option>
                    <option value="recovered">Recovered</option>
                </optgroup>
                {showBackfills && (
                    <optgroup label="Backfills">
                        <option value="pending">Pending</option>
                        <option value="completed">Completed</option>
                        <option value="partial">Partial</option>
                        <option value="canceled">Canceled</option>
                    </optgroup>
                )}
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
            mode="runs"
            search={search}
            onSearch={setSearch}
            page={page}
            setPage={setPage}
            total={total}
            pageCount={pageCount}
            pageSize={pageSize}
            onRefresh={() => refetchAllRuns()}
            loading={loading}
            isFetching={isFetching}
            hasMore={hasNextPage}
            onLoadMore={() => fetchNextPage()}
            isLoadingMore={isFetchingNextPage}
            filters={filters}
        >
            <WorkspaceFilterChips chips={filterChips} />

            <div className="card">
                {loading ? (
                    <TableSkeleton rows={10} cols={showBackfills ? 7 : 6} />
                ) : isError ? (
                    <EmptyState
                        icon={AlertTriangle}
                        tone="error"
                        title="Couldn't load runs"
                        description={formatApiErrorMessage(error instanceof Error ? error.message : String(error ?? ''))}
                        action={<Button variant="secondary" onClick={() => refetchAllRuns()}>Retry</Button>}
                    />
                ) : total === 0 ? (
                    <EmptyState
                        icon={hasActiveFilters || search ? Search : Inbox}
                        title={hasActiveFilters || search ? 'No matching runs' : 'No runs found'}
                        description={hasActiveFilters || search
                            ? 'Try adjusting your filters or search'
                            : 'Pipeline runs will appear here when executions complete'}
                    />
                ) : (
                    <table className="table-full" aria-label="Pipeline executions">
                        <thead>
                            <tr className="table-header">
                                <SortableHeader label="Pipeline" sortKey="pipeline_name" currentSort={sort} onSort={handleSort} />
                                <th className="arv-table-cell">Execution</th>
                                <SortableHeader label="Status" sortKey="status" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Date" sortKey="date" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Duration" sortKey="duration_ms" currentSort={sort} onSort={handleSort} />
                                <SortableHeader label="Started" sortKey="started_at" currentSort={sort} onSort={handleSort} />
                                {showBackfills && <th className="arv-table-cell">Backfill</th>}
                            </tr>
                        </thead>
                        <tbody>
                            {paged.map((run, i: number) => {
                                const isBackfill = run.kind === 'backfill';
                                const rowKey = isBackfill
                                    ? `bf-${run.backfill_id}`
                                    : `ex-${run.pipeline_execution || i}`;
                                return (
                                <tr key={rowKey} className="border-b">
                                    <td className="p-md">
                                        <span
                                            className="text-accent clickable font-medium"
                                            onClick={() => handlePipelineClick(run)}
                                        >
                                            {run.pipeline_name}
                                        </span>
                                    </td>
                                    <td className="table-cell-mono">
                                        {isBackfill ? (
                                            <span className="text-muted" title="Backfill — multiple executions">
                                                <ActionIcons.backfill size={12} /> {run.completed_partitions ?? 0}/{run.total_partitions ?? 0} partitions
                                            </span>
                                        ) : (
                                            <>{run.pipeline_execution_short || run.pipeline_execution?.substring(0, 8)}...</>
                                        )}
                                    </td>
                                    <td className="p-md">
                                        {isBackfill ? (
                                            <span className={`bl-status-pill bl-status-pill--${run.status}`}>
                                                {String(run.status) === 'partial'
                                                    ? `partial (${run.completed_partitions ?? 0}/${run.total_partitions ?? 0})`
                                                    : run.status}
                                            </span>
                                        ) : (
                                            <span className={`task-status-badge ${run.status}`}>
                                                {run.status}
                                            </span>
                                        )}
                                    </td>
                                    <td className="table-cell-mono">{isBackfill ? '—' : run.date}</td>
                                    <td className="table-cell-mono">
                                        {formatDuration(run.duration_ms)}
                                    </td>
                                    <td className="p-md text-sm text-muted">
                                        {run.started_at ? new Date(run.started_at).toLocaleString() : '-'}
                                    </td>
                                    {showBackfills && (
                                        <td className="table-cell-mono">
                                            {run.backfill_id ? (
                                                <span
                                                    className="text-accent clickable"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        router.push(`/backfills/${run.backfill_id}/`);
                                                    }}
                                                    title={`View backfill ${run.backfill_id}`}
                                                >
                                                    <ActionIcons.backfill size={12} /> {run.backfill_id}
                                                </span>
                                            ) : (
                                                <span className="text-muted">—</span>
                                            )}
                                        </td>
                                    )}
                                </tr>
                                );
                            })}
                        </tbody>
                    </table>
                )}
            </div>
        </HistoryChrome>
    );
}

export default AllRunsView;
