import React from 'react';
import { useClientRoute } from '@/hooks/useClientRoute';
import { Activity, ListTodo, Search, ChevronLeft, ChevronRight } from '../utils/icons';
import { Button } from '@/components/ui/button';
import { RefreshButton } from './RefreshButton';

interface HistoryChromeProps {
    /** Which sub-view is active — drives the toggle and the client-side route it pushes. */
    mode: 'runs' | 'tasks';
    search: string;
    onSearch: (value: string) => void;
    page: number;
    setPage: (page: number) => void;
    total: number;
    pageCount: number;
    pageSize: number;
    /** View-specific filter controls (pipeline/status/date …), rendered in the toolbar. */
    filters?: React.ReactNode;
    onRefresh: () => void;
    loading?: boolean;
    /** Spins the refresh icon and disables it while a fetch is in flight. */
    isFetching?: boolean;
    /**
     * The API still has older rows. `total` counts what has been loaded, so this is
     * what keeps the count honest: "50+" while more exists, a plain "73" once the
     * feed is exhausted. Never guessed client-side — it comes from the API's cursor.
     */
    hasMore?: boolean;
    /** Loads the next (older) page and appends it. */
    onLoadMore?: () => void;
    isLoadingMore?: boolean;
    /** Optional ref to the search input, so a view can wire a focus-filter shortcut to it. */
    searchRef?: React.Ref<HTMLInputElement>;
    children: React.ReactNode;
}

/**
 * Shared chrome for the History view (All runs / All tasks merged, ADR #111 client routing).
 * Renders the title, the Runs/Tasks toggle, a reference-style toolbar (search + the view's own
 * filters), the table (children), and a pagination footer. Each view keeps its own data,
 * filtering and sorting and passes the sorted-then-searched-then-paged rows as children.
 *
 * Two paginations meet in the footer, and they are not the same thing: the pager walks the
 * rows already loaded, `Show older` asks the API for the next page of the feed itself.
 */
export function HistoryChrome({
    mode, search, onSearch, page, setPage, total, pageCount, pageSize, filters, onRefresh, loading, isFetching,
    hasMore, onLoadMore, isLoadingMore, searchRef, children,
}: HistoryChromeProps) {
    const { push } = useClientRoute();
    const from = total === 0 ? 0 : page * pageSize + 1;
    const to = Math.min((page + 1) * pageSize, total);

    return (
        <div className="panel-section">
            <div className="hc-header">
                <h2 className="title-lg">History</h2>
                <RefreshButton onRefresh={onRefresh} isFetching={isFetching} size="sm" iconSize={14} />
            </div>

            <div className="hc-toolbar">
                <div className="hc-toggle" role="tablist" aria-label="Runs or tasks">
                    <button
                        type="button" role="tab" aria-selected={mode === 'runs'}
                        tabIndex={mode === 'runs' ? 0 : -1}
                        className={`hc-toggle-btn ${mode === 'runs' ? 'active' : ''}`}
                        onClick={() => { if (mode !== 'runs') push('/runs/'); }}
                    >
                        <Activity size={14} /> Runs
                    </button>
                    <button
                        type="button" role="tab" aria-selected={mode === 'tasks'}
                        tabIndex={mode === 'tasks' ? 0 : -1}
                        className={`hc-toggle-btn ${mode === 'tasks' ? 'active' : ''}`}
                        onClick={() => { if (mode !== 'tasks') push('/tasks/'); }}
                    >
                        <ListTodo size={14} /> Tasks
                    </button>
                </div>

                <div className="hc-search">
                    <Search size={15} aria-hidden="true" />
                    <input
                        ref={searchRef}
                        type="text"
                        value={search}
                        onChange={e => onSearch(e.target.value)}
                        placeholder="Search…"
                        aria-label={`Search ${mode}`}
                    />
                </div>

                {filters}
            </div>

            {children}

            {(loading || total > 0 || hasMore) && (
                <div className="hc-pagination">
                    <div className="hc-pagination-info">
                        <span className="text-muted text-sm">
                            {loading ? <span className="loading-spinner-sm" /> : `${total}${hasMore ? '+' : ''} ${mode}`}
                        </span>
                        {!loading && pageCount > 1 && (
                            <div className="hc-pager">
                                <button
                                    type="button" className="hc-pager-btn"
                                    disabled={page <= 0} onClick={() => setPage(page - 1)}
                                    aria-label="Previous page"
                                >
                                    <ChevronLeft size={16} />
                                </button>
                                <span className="text-sm text-muted">{from}–{to} of {total}</span>
                                <button
                                    type="button" className="hc-pager-btn"
                                    disabled={page >= pageCount - 1} onClick={() => setPage(page + 1)}
                                    aria-label="Next page"
                                >
                                    <ChevronRight size={16} />
                                </button>
                            </div>
                        )}
                    </div>
                    {!loading && hasMore && (
                        <Button
                            variant="secondary"
                            size="sm"
                            onClick={onLoadMore}
                            disabled={isLoadingMore}
                        >
                            {isLoadingMore ? 'Loading…' : `Show older ${mode}`}
                        </Button>
                    )}
                </div>
            )}
        </div>
    );
}
