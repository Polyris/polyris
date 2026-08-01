import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useAppStore } from '../stores/useAppStore';

const mockFetchNextPage = vi.fn();
/** Infinite-query shape: the feed arrives page by page (see useAllTasksQuery). */
const mockQueryData = {
    data: { pages: [] as Array<{ tasks: Record<string, unknown>[]; next: string | null }> },
    isLoading: false,
    refetch: vi.fn(),
    fetchNextPage: mockFetchNextPage,
    hasNextPage: false,
    isFetchingNextPage: false,
};
const { mockPush } = vi.hoisted(() => ({ mockPush: vi.fn() }));

vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: '/tasks/', push: mockPush, replace: vi.fn() }),
}));
vi.mock('@/hooks/queries', () => ({
    useAllTasksQuery: () => mockQueryData,
    useAllRunsQuery: () => ({
        data: { pages: [] }, isLoading: false, refetch: vi.fn(),
        fetchNextPage: vi.fn(), hasNextPage: false, isFetchingNextPage: false,
    }),
    usePipelinesQuery: () => ({ data: mockPipelines }),
}));
vi.mock('@/utils/icons', () => ({
    Calendar: () => <span data-testid="icon-calendar" />,
    ChevronLeft: () => <span data-testid="icon-chevronleft" />,
    ChevronRight: () => <span data-testid="icon-chevronright" />,
    Activity: () => <span data-testid="icon-activity" />,
    Search: () => <span data-testid="icon-search" />,
    Inbox: () => <span data-testid="icon-inbox" />,
    ArrowUp: () => <span>↑</span>,
    ArrowDown: () => <span>↓</span>,
    StatusIcon: ({ status }: { status: string }) => <span data-testid={`status-${status}`} />,
    ListTodo: () => <span data-testid="icon-list" />,
    RefreshCw: () => <span data-testid="icon-refresh" />,
    AlertTriangle: () => <span data-testid="icon-alert" />,
    X: () => <span data-testid="icon-x" />,
}));
vi.mock('../utils/icons', () => ({
    Calendar: () => <span data-testid="icon-calendar" />,
    ChevronLeft: () => <span data-testid="icon-chevronleft" />,
    ChevronRight: () => <span data-testid="icon-chevronright" />,
    Activity: () => <span data-testid="icon-activity" />,
    Search: () => <span data-testid="icon-search" />,
    Inbox: () => <span data-testid="icon-inbox" />,
    ArrowUp: () => <span>↑</span>,
    ArrowDown: () => <span>↓</span>,
    StatusIcon: ({ status }: { status: string }) => <span data-testid={`status-${status}`} />,
    ListTodo: () => <span data-testid="icon-list" />,
    RefreshCw: () => <span data-testid="icon-refresh" />,
    AlertTriangle: () => <span data-testid="icon-alert" />,
    X: () => <span data-testid="icon-x" />,
}));
vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => <button onClick={props.onClick as () => void} disabled={props.disabled as boolean}>{props.children as React.ReactNode}</button>,
}));
vi.mock('./Skeletons', () => ({
    TableSkeleton: () => <div data-testid="skeleton" />,
}));

import { AllTasksView } from './AllTasksView';

const mockTasks = [
    { task_name: 'extract', pipeline_name: 'acme-daily', status: 'success', execution_name: 'exec-001', started_at: '2024-01-15T08:00:00Z', duration_ms: 30000, date: '2024-01-15' },
    { task_name: 'transform', pipeline_name: 'acme-daily', status: 'failed', execution_name: 'exec-002', started_at: '2024-01-15T08:01:00Z', duration_ms: 15000, date: '2024-01-15' },
    { task_name: 'load', pipeline_name: 'shopmart-weekly', status: 'running', execution_name: 'exec-003', started_at: '2024-01-15T08:02:00Z', duration_ms: 0, date: '2024-01-15' },
];

const mockPipelines = [
    { name: 'acme-daily' },
    { name: 'shopmart-weekly' },
] as Array<{ name: string }>;

const defaultProps = {
    onPipelineClick: vi.fn(),
};

function setup(overrides: {
    tasks?: Record<string, unknown>[];
    pages?: Array<{ tasks: Record<string, unknown>[]; next: string | null }>;
    loading?: boolean;
    hasNextPage?: boolean;
    isFetchingNextPage?: boolean;
} = {}) {
    const store = useAppStore.getState();
    store.setTaskFilter({ status: '', date: '', pipeline: '', taskName: '' });
    mockQueryData.data = { pages: overrides.pages ?? [{ tasks: overrides.tasks ?? mockTasks, next: null }] };
    mockQueryData.isLoading = overrides.loading ?? false;
    mockQueryData.hasNextPage = overrides.hasNextPage ?? false;
    mockQueryData.isFetchingNextPage = overrides.isFetchingNextPage ?? false;
    mockFetchNextPage.mockClear();
    defaultProps.onPipelineClick.mockClear();
}

describe('AllTasksView', () => {
    beforeEach(() => setup());

    describe('rendering', () => {
        it('renders table with tasks', () => {
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('extract')).toBeInTheDocument();
            expect(screen.getByText('transform')).toBeInTheDocument();
            expect(screen.getByText('load')).toBeInTheDocument();
        });

        it('shows task count', () => {
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('3 tasks')).toBeInTheDocument();
        });

        it('shows loading skeleton when loading', () => {
            setup({ tasks: [], loading: true });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByTestId('skeleton')).toBeInTheDocument();
        });

        it('shows empty state when no tasks', () => {
            setup({ tasks: [] });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('No tasks found')).toBeInTheDocument();
        });

        it('table has aria-label', () => {
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByRole('table')).toHaveAttribute('aria-label', 'Task executions');
        });
    });

    describe('sorting', () => {
        it('sorts by column when header clicked', () => {
            render(<AllTasksView {...defaultProps} />);
            const taskHeader = screen.getByText('Task');
            fireEvent.click(taskHeader.closest('th')!);
            expect(taskHeader.closest('th')).toHaveAttribute('aria-sort', 'descending');
        });

        it('toggles sort direction', () => {
            render(<AllTasksView {...defaultProps} />);
            const header = screen.getByText('Task').closest('th')!;
            fireEvent.click(header);
            expect(header).toHaveAttribute('aria-sort', 'descending');
            fireEvent.click(header);
            expect(header).toHaveAttribute('aria-sort', 'ascending');
        });
    });

    describe('filtering', () => {
        it('has a search input', () => {
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByPlaceholderText(/search/i)).toBeInTheDocument();
        });

        it('updates store when status filter changed', () => {
            render(<AllTasksView {...defaultProps} />);
            const statusSelect = screen.getByDisplayValue('All Statuses');
            fireEvent.change(statusSelect, { target: { value: 'failed' } });
            expect(useAppStore.getState().taskFilter.status).toBe('failed');
        });
    });

    describe('interactions', () => {
        it('calls onPipelineClick when pipeline name clicked', () => {
            render(<AllTasksView {...defaultProps} />);
            const table = screen.getByRole('table');
            const pipelineLinks = Array.from(table.querySelectorAll('.clickable'));
            expect(pipelineLinks.length).toBeGreaterThan(0);
            fireEvent.click(pipelineLinks[0]);
            expect(defaultProps.onPipelineClick).toHaveBeenCalledWith(
                expect.objectContaining({ name: 'acme-daily' }),
                '2024-01-15'
            );
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // v0.78.3+ — keyboard shortcuts (ADR #64).
    // ──────────────────────────────────────────────────────────────────────
    describe('keyboard shortcuts', () => {
        it('ctrl+r calls refetch on the tasks query', () => {
            mockQueryData.refetch.mockClear();
            render(<AllTasksView {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'r', ctrlKey: true });
            expect(mockQueryData.refetch).toHaveBeenCalled();
        });

        it('/ focuses the search input', () => {
            render(<AllTasksView {...defaultProps} />);
            const searchInput = screen.getByPlaceholderText(/search/i);
            fireEvent.keyDown(document, { key: '/' });
            expect(document.activeElement).toBe(searchInput);
        });
    });
    // ──────────────────────────────────────────────────────────────────────
    // Cursor paging — same contract as the runs half (one dialect).
    // ──────────────────────────────────────────────────────────────────────
    describe('paging', () => {
        it('renders rows from every page loaded so far', () => {
            setup({ pages: [
                { tasks: [mockTasks[0]], next: '2024-01-15T08:00:00Z' },
                { tasks: [mockTasks[2]], next: null },
            ] });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('extract')).toBeInTheDocument();
            expect(screen.getByText('load')).toBeInTheDocument();
            expect(screen.getByText('2 tasks')).toBeInTheDocument();
        });

        it('marks the count as partial while the API has older tasks', () => {
            setup({ hasNextPage: true });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('3+ tasks')).toBeInTheDocument();
        });

        it('drops the + once the feed is exhausted', () => {
            setup({ hasNextPage: false });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByText('3 tasks')).toBeInTheDocument();
        });

        it('loads the older page on Show older tasks', () => {
            setup({ hasNextPage: true });
            render(<AllTasksView {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: 'Show older tasks' }));
            expect(mockFetchNextPage).toHaveBeenCalled();
        });

        it('offers nothing to load when nothing older exists', () => {
            setup({ hasNextPage: false });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.queryByRole('button', { name: 'Show older tasks' })).not.toBeInTheDocument();
        });

        it('disables the button while the older page is in flight', () => {
            setup({ hasNextPage: true, isFetchingNextPage: true });
            render(<AllTasksView {...defaultProps} />);
            expect(screen.getByRole('button', { name: 'Loading…' })).toBeDisabled();
        });
    });

});
