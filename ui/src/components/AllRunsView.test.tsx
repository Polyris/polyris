import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { useAppStore } from '../stores/useAppStore';

const mockRefetch = vi.fn();
const mockFetchNextPage = vi.fn();
/** Infinite-query shape: the feed arrives page by page (see useAllRunsQuery). */
const mockQueryData = {
    data: { pages: [] as Array<{ runs: Record<string, unknown>[]; next: string | null }> },
    isLoading: false,
    refetch: mockRefetch,
    fetchNextPage: mockFetchNextPage,
    hasNextPage: false,
    isFetchingNextPage: false,
};
const { mockPush, paidSurfaceMock } = vi.hoisted(() => ({ mockPush: vi.fn(), paidSurfaceMock: {} as Record<string, unknown> }));

vi.mock('@/ee-active.generated', () => ({ paidSurface: paidSurfaceMock }));
vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: '/runs/', push: mockPush, replace: vi.fn() }),
}));
vi.mock('@/hooks/queries', () => ({
    useAllRunsQuery: () => mockQueryData,
    usePipelinesQuery: () => ({ data: mockPipelines }),
}));
vi.mock('@/utils/icons', () => ({
    Calendar: () => <span data-testid="icon-calendar" />,
    ChevronLeft: () => <span data-testid="icon-chevronleft" />,
    ChevronRight: () => <span data-testid="icon-chevronright" />,
    X: () => <span data-testid="icon-x" />,
    Activity: () => <span data-testid="icon-activity" />,
    ListTodo: () => <span data-testid="icon-listtodo" />,
    RefreshCw: () => <span data-testid="icon-refresh" />,
    Search: () => <span data-testid="icon-search" />,
    Inbox: () => <span data-testid="icon-inbox" />,
    Rocket: () => <span data-testid="icon-rocket" />,
    ArrowUp: () => <span>↑</span>,
    ArrowDown: () => <span>↓</span>,
    StatusIcon: ({ status }: { status: string }) => <span data-testid={`status-${status}`} />,
    ActionIcons: { backfill: () => <span data-testid="icon-backfill" /> },
}));
vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => <button onClick={props.onClick as () => void} disabled={props.disabled as boolean} aria-label={props['aria-label'] as string} title={props.title as string}>{props.children as React.ReactNode}</button>,
}));
vi.mock('./Skeletons', () => ({
    TableSkeleton: () => <div data-testid="skeleton" />,
}));

import { AllRunsView } from './AllRunsView';

const mockRuns = [
    { execution_id: 'exec-001', pipeline_name: 'acme-daily', status: 'success', date: '2024-01-15', started_at: '2024-01-15T08:00:00Z', duration_ms: 60000 },
    { execution_id: 'exec-002', pipeline_name: 'shopmart-weekly', status: 'failed', date: '2024-01-15', started_at: '2024-01-15T09:00:00Z', duration_ms: 30000 },
    { execution_id: 'exec-003', pipeline_name: 'acme-daily', status: 'running', date: '2024-01-15', started_at: '2024-01-15T10:00:00Z', duration_ms: 0 },
];

const mockPipelines = [
    { name: 'acme-daily' },
    { name: 'shopmart-weekly' },
] as Array<{ name: string }>;

const defaultProps = {
    onPipelineClick: vi.fn(),
};

function setup(overrides: {
    runs?: Record<string, unknown>[];
    pages?: Array<{ runs: Record<string, unknown>[]; next: string | null }>;
    loading?: boolean;
    hasNextPage?: boolean;
    isFetchingNextPage?: boolean;
} = {}) {
    // Reset the URL: the view seeds its filter from the query string on mount, and jsdom
    // carries the URL across tests — so without this, whatever the previous test's filter
    // wrote would be re-applied here and quietly overwrite what this one sets up.
    window.history.replaceState({}, '', '/runs/');
    const store = useAppStore.getState();
    store.setDate('2024-01-15');
    store.setRunFilter({ status: '', pipeline: '', date: '' });
    mockQueryData.data = { pages: overrides.pages ?? [{ runs: overrides.runs ?? mockRuns, next: null }] };
    mockQueryData.isLoading = overrides.loading ?? false;
    mockQueryData.hasNextPage = overrides.hasNextPage ?? false;
    mockQueryData.isFetchingNextPage = overrides.isFetchingNextPage ?? false;
    mockRefetch.mockClear();
    mockFetchNextPage.mockClear();
    defaultProps.onPipelineClick.mockClear();
    mockPush.mockClear();
    for (const k of Object.keys(paidSurfaceMock)) delete paidSurfaceMock[k];
}

describe('AllRunsView', () => {
    beforeEach(() => setup());

    describe('rendering', () => {
        it('renders table with runs', () => {
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getAllByText('acme-daily').length).toBeGreaterThanOrEqual(2);
            expect(screen.getAllByText('shopmart-weekly').length).toBeGreaterThanOrEqual(1);
        });

        it('shows run count in header', () => {
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByText('3 runs')).toBeInTheDocument();
        });

        it('shows loading skeleton when loading', () => {
            setup({ runs: [], loading: true });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByTestId('skeleton')).toBeInTheDocument();
        });

        it('shows empty state when no runs', () => {
            setup({ runs: [] });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByText('No runs found')).toBeInTheDocument();
        });

        it('table has aria-label', () => {
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByRole('table')).toHaveAttribute('aria-label', 'Pipeline executions');
        });
    });

    describe('sorting', () => {
        it('sorts by column when header clicked', () => {
            render(<AllRunsView {...defaultProps} />);
            const pipelineHeader = screen.getByText('Pipeline');
            fireEvent.click(pipelineHeader.closest('th')!);
            expect(pipelineHeader.closest('th')).toHaveAttribute('aria-sort', 'descending');
        });

        it('toggles sort direction on second click', () => {
            render(<AllRunsView {...defaultProps} />);
            const header = screen.getByText('Pipeline').closest('th')!;
            fireEvent.click(header);
            expect(header).toHaveAttribute('aria-sort', 'descending');
            fireEvent.click(header);
            expect(header).toHaveAttribute('aria-sort', 'ascending');
        });

        it('supports keyboard sorting with Enter', () => {
            render(<AllRunsView {...defaultProps} />);
            const header = screen.getByText('Pipeline').closest('th')!;
            fireEvent.keyDown(header, { key: 'Enter' });
            expect(header).toHaveAttribute('aria-sort', 'descending');
        });
    });

    describe('filtering', () => {
        it('updates store when status filter changed', () => {
            render(<AllRunsView {...defaultProps} />);
            const statusSelect = screen.getByDisplayValue('All Statuses');
            fireEvent.change(statusSelect, { target: { value: 'failed' } });
            expect(useAppStore.getState().runFilter.status).toBe('failed');
        });

        it('calls refetch when refresh clicked', () => {
            render(<AllRunsView {...defaultProps} />);
            const refreshBtn = screen.getByRole('button', { name: 'Refresh' });
            fireEvent.click(refreshBtn);
            expect(mockRefetch).toHaveBeenCalled();
        });
    });

    describe('interactions', () => {
        it('calls onPipelineClick when pipeline name clicked', () => {
            render(<AllRunsView {...defaultProps} />);
            const table = screen.getByRole('table');
            const pipelineLinks = Array.from(table.querySelectorAll('.clickable'));
            expect(pipelineLinks.length).toBeGreaterThan(0);
            fireEvent.click(pipelineLinks[0]);
            expect(defaultProps.onPipelineClick).toHaveBeenCalledWith(
                expect.objectContaining({ name: 'acme-daily' }),
                expect.objectContaining({ pipeline_name: 'acme-daily' })
            );
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // v0.78.3+ — keyboard shortcuts (ADR #64).
    // ──────────────────────────────────────────────────────────────────────
    describe('keyboard shortcuts', () => {
        it('ctrl+r calls refetch', () => {
            mockRefetch.mockClear();
            render(<AllRunsView {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'r', ctrlKey: true });
            expect(mockRefetch).toHaveBeenCalled();
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // v0.86.0 — unified Run/Activity feed: Backfills as first-class rows (ADR #95).
    // ──────────────────────────────────────────────────────────────────────
    describe('unified feed — backfill rows (ADR #95)', () => {
        const mixedRuns = [
            { kind: 'execution', pipeline_name: 'acme-daily', pipeline_execution: 'acme-2024-01-15-abc123', pipeline_execution_short: 'abc123', status: 'succeeded', date: '2024-01-15', started_at: '2024-01-15T08:00:00Z', duration_ms: 60000 },
            { kind: 'backfill', id: 'bf-xyz789', backfill_id: 'bf-xyz789', pipeline_name: 'shopmart-weekly', status: 'completed', started_at: '2024-01-15T09:00:00Z', finished_at: '2024-01-15T09:30:00Z', total_partitions: 7, completed_partitions: 7, failed_partitions: 0, duration_ms: 1800000, date: null },
            { kind: 'backfill', id: 'bf-part', backfill_id: 'bf-part', pipeline_name: 'acme-daily', status: 'partial', total_partitions: 10, completed_partitions: 6, failed_partitions: 4, started_at: '2024-01-15T07:00:00Z' },
        ];

        it('renders a backfill status pill using the backfill vocabulary', () => {
            setup({ runs: mixedRuns });
            const { container } = render(<AllRunsView {...defaultProps} />);
            expect(container.querySelector('.bl-status-pill--completed')).toBeTruthy();
            expect(container.querySelector('.bl-status-pill--partial')).toBeTruthy();
        });

        it('shows partition progress instead of an execution id for backfill rows', () => {
            setup({ runs: mixedRuns });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByText(/7\/7 partitions/)).toBeInTheDocument();
        });

        it('renders the partial ratio in the status pill', () => {
            setup({ runs: mixedRuns });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByText('partial (6/10)')).toBeInTheDocument();
        });

        it('navigates to the backfill detail page when a backfill link is clicked', () => {
            setup({ runs: mixedRuns });
            paidSurfaceMock.BackfillsView = function BackfillsView() { return null; }; // Team surface enables the backfill column
            const { container } = render(<AllRunsView {...defaultProps} />);
            const bfLink = container.querySelector('[title="View backfill bf-xyz789"]');
            expect(bfLink).toBeTruthy();
            fireEvent.click(bfLink!);
            expect(mockPush).toHaveBeenCalledWith('/backfills/bf-xyz789/');
        });

        it('exposes backfill statuses in the status filter', () => {
            setup({ runs: mixedRuns });
            paidSurfaceMock.BackfillsView = function BackfillsView() { return null; }; // Team surface enables the backfill optgroup
            const { container } = render(<AllRunsView {...defaultProps} />);
            // Read option values directly (robust to selected-value carryover
            // from prior tests via useUrlSync).
            const values = Array.from(container.querySelectorAll('option')).map(o => o.value);
            expect(values).toEqual(expect.arrayContaining(['completed', 'partial', 'canceled', 'pending']));
        });

        it('hides the backfill column and statuses in the open-source build (paidSurface absent)', () => {
            setup({ runs: mixedRuns }); // no paidSurface.BackfillsView
            const { container } = render(<AllRunsView {...defaultProps} />);
            expect(screen.queryByText('Backfill')).not.toBeInTheDocument();
            expect(container.querySelector('[title="View backfill bf-xyz789"]')).toBeNull();
            const values = Array.from(container.querySelectorAll('option')).map(o => o.value);
            expect(values).not.toContain('completed');
        });
    });

    describe('inline date picker', () => {
        it('renders an inline date picker showing the current date (mirrors All Tasks)', () => {
            // Driven by this workspace's own filter, exactly like All Tasks (ADR #106) —
            // it used to read the Pipeline page's global scope instead.
            useAppStore.getState().setRunFilter({ status: '', pipeline: '', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);
            // The app-styled DatePicker shows the formatted date on its trigger button.
            expect(screen.getByText('Jan 15, 2024')).toBeInTheDocument();
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // Cursor paging — the feed arrives page by page and says so honestly.
    // ──────────────────────────────────────────────────────────────────────
    describe('paging', () => {
        it('renders rows from every page loaded so far', () => {
            setup({ pages: [
                { runs: [mockRuns[0]], next: '2024-01-15T08:00:00Z' },
                { runs: [mockRuns[1]], next: null },
            ] });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getAllByText('acme-daily').length).toBeGreaterThanOrEqual(1);
            expect(screen.getAllByText('shopmart-weekly').length).toBeGreaterThanOrEqual(1);
            expect(screen.getByText('2 runs')).toBeInTheDocument();
        });

        it('marks the count as partial while the API has older runs', () => {
            setup({ hasNextPage: true });
            render(<AllRunsView {...defaultProps} />);
            // "3+", not "3": three loaded, more behind them. The old feed said "50 runs"
            // when it meant "the first 50 of who knows how many".
            expect(screen.getByText('3+ runs')).toBeInTheDocument();
        });

        it('drops the + once the feed is exhausted', () => {
            setup({ hasNextPage: false });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getByText('3 runs')).toBeInTheDocument();
        });

        it('loads the older page on Show older runs', () => {
            setup({ hasNextPage: true });
            render(<AllRunsView {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: 'Show older runs' }));
            expect(mockFetchNextPage).toHaveBeenCalled();
        });

        it('offers nothing to load when nothing older exists', () => {
            setup({ hasNextPage: false });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.queryByRole('button', { name: 'Show older runs' })).not.toBeInTheDocument();
        });

        it('disables the button while the older page is in flight', () => {
            setup({ hasNextPage: true, isFetchingNextPage: true });
            render(<AllRunsView {...defaultProps} />);
            const btn = screen.getByRole('button', { name: 'Loading…' });
            expect(btn).toBeDisabled();
        });

        it('can still reach older runs when the search matches none of the loaded ones', () => {
            setup({ hasNextPage: true });
            render(<AllRunsView {...defaultProps} />);
            fireEvent.change(screen.getByLabelText('Search runs'), { target: { value: 'zzz' } });
            expect(screen.getByText('No matching runs')).toBeInTheDocument();
            expect(screen.getByRole('button', { name: 'Show older runs' })).toBeInTheDocument();
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // The date is this workspace's own filter, not the Pipeline page's scope.
    // Reported: clear the date here, open a pipeline -> bare graph, no banner,
    // while the run sat right there. One value, two meanings (ADR #106).
    // ──────────────────────────────────────────────────────────────────────
    describe('date filter', () => {
        function clearTheDatePicker() {
            fireEvent.click(screen.getByLabelText('Filter by date'));
            const clear = screen.getAllByRole('button', { name: 'Clear' })
                .find(b => b.className.includes('dp-foot-btn'))!;
            fireEvent.click(clear);
        }

        it('never touches the pipeline page scope', () => {
            setup();
            useAppStore.getState().setDate('2024-01-15');
            useAppStore.getState().setRunFilter({ status: '', pipeline: '', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);

            clearTheDatePicker();

            expect(useAppStore.getState().runFilter.date).toBe('');
            expect(useAppStore.getState().date).toBe('2024-01-15');   // untouched
        });

        it('clears to "all dates" rather than to a date called ""', () => {
            setup();
            useAppStore.getState().setRunFilter({ status: '', pipeline: '', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);

            clearTheDatePicker();

            expect(useAppStore.getState().runFilter.date).toBe('');
        });

        it('counts as an active filter, like every other filter here', () => {
            setup();
            useAppStore.getState().setRunFilter({ status: '', pipeline: '', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);
            expect(screen.getAllByRole('button', { name: 'Clear' }).length).toBeGreaterThan(0);
        });

        it('is removable from the chip row, like every other filter here', () => {
            setup();
            useAppStore.getState().setRunFilter({ status: '', pipeline: '', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);

            const chips = within(screen.getByRole('group', { name: 'Active filters' }));
            expect(chips.getByText('2024-01-15')).toBeInTheDocument();

            fireEvent.click(chips.getByTitle('Remove Date filter'));
            expect(useAppStore.getState().runFilter.date).toBe('');
        });

        it('Clear resets it along with the rest', () => {
            setup();
            useAppStore.getState().setRunFilter({ status: 'failed', pipeline: 'x', date: '2024-01-15' });
            render(<AllRunsView {...defaultProps} />);

            const toolbarClear = screen.getAllByRole('button', { name: 'Clear' })
                .find(b => !b.className.includes('dp-foot-btn'))!;
            fireEvent.click(toolbarClear);

            expect(useAppStore.getState().runFilter).toEqual({ status: '', pipeline: '', date: '' });
        });
    });

});
