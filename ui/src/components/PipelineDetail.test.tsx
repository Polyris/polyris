import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useAppStore } from '../stores/useAppStore';
import { createPipeline, createTask, createDAG, createExecution } from '../test/factories';
import type { PipelineWithUI, DAG } from '../types';

// ─── Query mocks ─────────────────────────────────────────────────────────────
const mockTasks = [
    createTask({ task_name: 'init_config', status: 'success' }),
    createTask({ task_name: 'extract_data', status: 'success' }),
    createTask({ task_name: 'transform', status: 'running' }),
    createTask({ task_name: 'load_db', status: 'waiting' }),
];
const mockDag = createDAG();
const mockExecList = [createExecution()];

const mockDetailQuery = { data: { tasks: mockTasks, dag: mockDag, serverOffsetMs: 0, selectedExecution: null }, isLoading: false };
const mockRunsQuery: { data: unknown; fetchNextPage: () => void; hasNextPage: boolean; isFetchingNextPage: boolean; isLoading: boolean } = {
    data: { pages: [] }, fetchNextPage: vi.fn(), hasNextPage: false,
    isFetchingNextPage: false, isLoading: false,
};
const mockExecQuery = { data: mockExecList };
const mockPipelinesQuery = { data: [createPipeline({ recent_runs: [{ date: '2024-01-15' }] })] };
const mockTaskEventsQuery = { data: [], isLoading: false };

vi.mock('../hooks/queries', () => ({
    usePipelineDetailQuery: () => mockDetailQuery,
    usePipelineExecutionsQuery: () => mockExecQuery,
    // The history dropdown fetches its own runs (windowless, paginated).
    usePipelineRunsQuery: () => mockRunsQuery,
    usePipelinesQuery: () => mockPipelinesQuery,
    useTaskEventsQuery: () => mockTaskEventsQuery,
}));

const mockActions = {
    modal: { isOpen: false, action: null, title: '', message: '', icon: '', confirmText: '', confirmStyle: '', customContent: false, toRun: null, toSkip: null },
    triggerParams: '{}', setTriggerParams: vi.fn(),
    actionPending: false, pendingAction: null as string | null,
    handleRun: vi.fn(), handleStop: vi.fn(), handlePauseResume: vi.fn(),
    handleExtendPause: vi.fn(), handleRefresh: vi.fn(), handleTaskAction: vi.fn(), handleRunAction: vi.fn(),
    executeModalAction: vi.fn(), closeModal: vi.fn(),
};
vi.mock('../hooks', async () => {
    const actual = await vi.importActual<typeof import('../hooks')>('../hooks');
    return {
        ...actual,
        usePipelineActions: () => mockActions,
        useToast: () => ({ show: vi.fn() }),
        // Use the real useKeyboardShortcuts + SHORTCUTS so keydown events
        // dispatch correctly in tests (v0.78.3, ADR #64).
    };
});

vi.mock('@tanstack/react-query', async () => {
    const actual = await vi.importActual<typeof import('@tanstack/react-query')>('@tanstack/react-query');
    return { ...actual, useQueryClient: () => ({ invalidateQueries: vi.fn() }) };
});

// ─── Component mocks ─────────────────────────────────────────────────────────
vi.mock('./index', () => ({
    DAGGraph: ({ tasks, onSelectTask }: { tasks: { task_name: string }[]; onSelectTask?: (t: unknown) => void }) => (
        <div data-testid="dag-graph">
            {tasks?.map(t => (
                <div key={t.task_name} data-testid={`dag-node-${t.task_name}`} onClick={() => onSelectTask?.(t)}>
                    {t.task_name}
                </div>
            ))}
        </div>
    ),
    DAGSkeleton: () => <div data-testid="dag-skeleton">Loading...</div>,
    GanttSkeleton: () => <div data-testid="gantt-skeleton">Loading...</div>,
    ErrorBoundary: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
    ActionModal: () => null,
    BackfillModal: () => null,
    TaskDetailModal: () => null,
}));

// Team view-modes still arrive via the EE surface (ADR #99), not the barrel.
vi.mock('@/ee-active.generated', () => ({
    paidSurface: {
        CalendarView: () => <div data-testid="calendar-view">Calendar</div>,
        GanttChart: () => <div data-testid="gantt-chart">Gantt</div>,
    },
}));

// Pipeline actions provider is free (ADR #110) — stub it so the host renders with
// injected mock handlers; the real hook/modal have their own co-located tests.
vi.mock('@/components/PipelineActionsProvider', () => ({
    PipelineActionsProvider: ({ children }: { params: unknown; children: (a: typeof mockActions) => React.ReactNode }) => children(mockActions),
}));

vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, X: () => null, Zap: () => null }));
vi.mock('./DatePicker', () => ({
    // The real picker is a portal-rendered calendar; stub it to a plain input so
    // these tests exercise the dropdown's filtering, not the picker's internals.
    DatePicker: (props: Record<string, unknown>) => (
        <input
            aria-label={props.ariaLabel as string}
            value={props.value as string}
            onChange={e => (props.onChange as (v: string) => void)(e.target.value)}
        />
    ),
}));
vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => <button onClick={props.onClick as () => void} title={props.title as string} aria-label={props['aria-label'] as string} data-variant={props.variant}>{props.children as React.ReactNode}</button>,
}));
vi.mock('../lib/config', () => ({ default: { API_URL: '/api', POLLING_INTERVAL: 5000, AUTH_ENABLED: false } }));
vi.mock('../utils/api', () => ({ api: { get: vi.fn(), post: vi.fn() }, setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));
vi.mock('@/utils/api', () => ({ api: { get: vi.fn(), post: vi.fn() }, setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));
vi.mock('../utils/storage', () => ({ getFromStorage: vi.fn(), saveToStorage: vi.fn() }));
vi.mock('@/hooks/useAuth', () => ({ AuthProvider: ({ children }: any) => children, useAuth: () => ({ isSignedIn: true, user: { email: 'test@test.com' }, signOut: vi.fn() }), useAuthHeader: () => ({}), AUTH_STATE: { SIGNED_IN: 'signedIn', SIGNED_OUT: 'signedOut' } }));

import { PipelineDetail } from './PipelineDetail';

/** Set store state for PipelineDetail */
function setStore(overrides: Record<string, unknown> = {}) {
    const store = useAppStore.getState();
    const pipeline = (overrides.pipeline !== undefined ? overrides.pipeline : createPipeline()) as PipelineWithUI | null;
    store.setSelectedPipeline(pipeline);
    store.setSelectedExecution((overrides.selectedExecution ?? null) as null);
    store.setViewMode((overrides.viewMode ?? 'dag') as 'dag' | 'gantt' | 'calendar');
    store.setDate((overrides.date ?? '2024-01-15') as string);
    store.setExecutionPaused((overrides.executionPaused ?? false) as boolean);
    store.setDagViewSource((overrides.dagViewSource ?? 'run') as 'run' | 'current');
}

const defaultProps = {
    apiError: null as string | null,
    navigateToExecution: vi.fn(),
};

describe('PipelineDetail', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        setStore();
        useAppStore.getState().setExecutionPaused(false);
        mockActions.pendingAction = null;
        // Reset mock query data
        mockDetailQuery.data = { tasks: mockTasks, dag: mockDag, serverOffsetMs: 0, selectedExecution: null };
        mockDetailQuery.isLoading = false;
        mockPipelinesQuery.data = [createPipeline({ recent_runs: [{ date: '2024-01-15' }] })];
    });

    describe('rendering', () => {
        it('renders the DAG graph in dag viewMode', () => {
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByTestId('dag-graph')).toBeInTheDocument();
        });

        it('renders calendar in calendar viewMode', () => {
            setStore({ viewMode: 'calendar' });
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByTestId('calendar-view')).toBeInTheDocument();
        });

        it('renders gantt in gantt viewMode', () => {
            setStore({ viewMode: 'gantt' });
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByTestId('gantt-chart')).toBeInTheDocument();
        });

        it('shows loading skeleton when isLoading', () => {
            mockDetailQuery.isLoading = true;
            mockDetailQuery.data = { tasks: [], dag: null as unknown as DAG, serverOffsetMs: 0, selectedExecution: null };
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByTestId('dag-skeleton')).toBeInTheDocument();
        });
    });

    describe('task stats', () => {
        it('filters tasks to match DAG nodes', () => {
            render(<PipelineDetail {...defaultProps} />);
            // 1 running + 1 waiting → Stop button appears
            expect(screen.getByText(/Stop/)).toBeInTheDocument();
        });
    });

    describe('task selection', () => {
        it('updates store when a DAG node is clicked', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByTestId('dag-node-extract_data'));
            expect(useAppStore.getState().selectedTaskName).toBe('extract_data');
        });
    });

    describe('DAG-based filtering', () => {
        it('only shows tasks that are in the DAG', () => {
            mockDetailQuery.data = {
                ...mockDetailQuery.data,
                tasks: [...mockTasks, createTask({ task_name: 'orphan_task', status: 'success' })],
            };
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.queryByTestId('dag-node-orphan_task')).not.toBeInTheDocument();
            expect(screen.getByTestId('dag-node-extract_data')).toBeInTheDocument();
        });
    });

    describe('error handling', () => {
        it('shows error message', () => {
            render(<PipelineDetail {...defaultProps} apiError="API connection failed" />);
            expect(screen.getByText(/API connection failed/)).toBeInTheDocument();
        });
    });

    describe('view mode controls', () => {
        it('renders view mode toggle buttons', () => {
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByText(/DAG/i)).toBeInTheDocument();
            expect(screen.getByText(/Gantt/i)).toBeInTheDocument();
            expect(screen.getByText(/Calendar/i)).toBeInTheDocument();
        });

        it('updates store when view toggle clicked', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByText(/Gantt/i));
            expect(useAppStore.getState().viewMode).toBe('gantt');
        });
    });

    // v0.78.3+ — viewMode shortcuts. v0.78.5: switched from numeric to
    // letter keys (d/g/c) because numeric keys conflict with global
    // top-level navigation in App.tsx. See ADR #64.
    describe('keyboard shortcuts › viewMode', () => {
        it('pressing "g" switches to gantt viewMode', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'g' });
            expect(useAppStore.getState().viewMode).toBe('gantt');
        });

        it('pressing "c" switches to calendar viewMode', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'c' });
            expect(useAppStore.getState().viewMode).toBe('calendar');
        });

        it('pressing "d" switches back to dag viewMode', () => {
            setStore({ viewMode: 'gantt' });
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'd' });
            expect(useAppStore.getState().viewMode).toBe('dag');
        });
    });

    describe('action buttons', () => {
        const idleTasks = [
            createTask({ task_name: 'a', status: 'success' }),
            createTask({ task_name: 'b', status: 'success' }),
        ];

        it('shows Run when the pipeline is idle', () => {
            mockDetailQuery.data = { ...mockDetailQuery.data, tasks: idleTasks };
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByRole('button', { name: /run/i })).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: /pause/i })).not.toBeInTheDocument();
        });

        it('shows Pause and Stop while running', () => {
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByRole('button', { name: /pause/i })).toBeInTheDocument();
            expect(screen.getByRole('button', { name: /stop/i })).toBeInTheDocument();
            expect(screen.queryByRole('button', { name: /^\s*run\s*$/i })).not.toBeInTheDocument();
        });

        it('shows Resume and Stop while paused', () => {
            mockDetailQuery.data = { ...mockDetailQuery.data, tasks: [createTask({ task_name: 'transform', status: 'waiting_paused' })] };
            useAppStore.getState().setExecutionPaused(true);
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getAllByRole('button', { name: /resume/i }).length).toBeGreaterThan(0);
            expect(screen.getByRole('button', { name: /stop/i })).toBeInTheDocument();
        });

        it('shows a loading label while an action is pending', () => {
            mockDetailQuery.data = { ...mockDetailQuery.data, tasks: idleTasks };
            mockActions.pendingAction = 'run';
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByText(/Starting/i)).toBeInTheDocument();
        });

        it('renders the History trigger even with no executions on the date (regression: drawer must stay reachable to change the date)', () => {
            const prev = mockExecQuery.data;
            mockExecQuery.data = [];
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByRole('button', { name: /History/i })).toBeInTheDocument();
            mockExecQuery.data = prev;
        });

        it('calls handleRun when Run clicked', () => {
            mockDetailQuery.data = { ...mockDetailQuery.data, tasks: idleTasks };
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /run/i }));
            expect(mockActions.handleRun).toHaveBeenCalled();
        });
    });

    describe('history dropdown', () => {
        afterEach(() => {
            mockPipelinesQuery.data = [createPipeline({ recent_runs: [{ date: '2024-01-15' }] })];
            mockRunsQuery.data = { pages: [] };   // don't leak runs between tests
        });

        // The dropdown lists every run it can still see rather than only the
        // selected date's, so the old "Latest" escape hatch (and the empty-date
        // deadlock it worked around) are gone by construction.
        it('opens with no date filter, so every run is listed', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/i }));
            expect(screen.getByLabelText('Filter runs by date')).toHaveValue('');
            expect(screen.queryByText('Latest')).not.toBeInTheDocument();
        });

        it('offers no Clear until a date filter is applied', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/i }));
            // Unfiltered by default → nothing to clear.
            expect(screen.queryByText('Clear')).not.toBeInTheDocument();
        });

        // The picker is stubbed to a plain input (see the mock at the top), so these
        // exercise the dropdown's own filtering rather than the calendar widget.
        it('filtering by date narrows the list and the button count follows it', () => {
            mockRunsQuery.data = { pages: [{ executions: [
                { execution_id: 'e1', execution_short: 'e1', date: '2024-01-15', status: 'success' },
                { execution_id: 'e2', execution_short: 'e2', date: '2024-01-15', status: 'failed' },
                { execution_id: 'e3', execution_short: 'e3', date: '2024-01-10', status: 'success' },
            ], next: null }] };
            render(<PipelineDetail {...defaultProps} />);

            // Unfiltered: every run, and the count says so.
            expect(screen.getByRole('button', { name: 'History (3)' })).toBeInTheDocument();
            fireEvent.click(screen.getByRole('button', { name: /History/i }));
            expect(screen.getAllByText(/^e[123]\.\.\.$/)).toHaveLength(3);

            // Filter to a date → only that day's runs, count follows.
            fireEvent.change(screen.getByLabelText('Filter runs by date'), { target: { value: '2024-01-10' } });
            expect(screen.getAllByText(/^e[123]\.\.\.$/)).toHaveLength(1);
            expect(screen.getByRole('button', { name: 'History (1)' })).toBeInTheDocument();

            // Clear → back to everything.
            fireEvent.click(screen.getByText('Clear'));
            expect(screen.getAllByText(/^e[123]\.\.\.$/)).toHaveLength(3);
            expect(screen.getByRole('button', { name: 'History (3)' })).toBeInTheDocument();
        });

        it('tells you the filter is to blame when it hides everything', () => {
            mockRunsQuery.data = { pages: [{ executions: [
                { execution_id: 'e1', execution_short: 'e1', date: '2024-01-15', status: 'success' },
            ], next: null }] };
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/i }));

            fireEvent.change(screen.getByLabelText('Filter runs by date'), { target: { value: '2024-01-01' } });
            expect(screen.getByText('No runs on this date')).toBeInTheDocument();
        });

        it('says "No runs yet" when there are none (not "none on this date")', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/i }));
            // The list spans every date, so an empty list means no runs at all —
            // never the old "no executions for {date}" dead end.
            expect(screen.getByText('No runs yet')).toBeInTheDocument();
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // The empty-graph banner explains an empty graph, so it must be gated on the
    // graph's own data. It asked `executions.length === 0` — a different question,
    // to a different endpoint — and vanished whenever the two disagreed, leaving a
    // bare graph with no explanation and no way out (ADR #106 follow-up).
    // ──────────────────────────────────────────────────────────────────────
    describe('run / current definition toggle', () => {
        it('renders "Definition" and the History button as two separate controls', () => {
            render(<PipelineDetail {...defaultProps} />);
            expect(screen.getByText('Definition')).toBeInTheDocument();
            expect(screen.getByRole('button', { name: /History/ })).toBeInTheDocument();
        });

        it('reflects the active half via aria-pressed on both buttons', () => {
            render(<PipelineDetail {...defaultProps} />);
            // Default is 'run' — History side pressed, Definition not.
            expect(screen.getByText('Definition')).toHaveAttribute('aria-pressed', 'false');
            expect(screen.getByRole('button', { name: /History/ })).toHaveAttribute('aria-pressed', 'true');

            fireEvent.click(screen.getByText('Definition'));
            expect(screen.getByText('Definition')).toHaveAttribute('aria-pressed', 'true');
            expect(screen.getByRole('button', { name: /History/ })).toHaveAttribute('aria-pressed', 'false');
        });

        it('clicking "Definition" updates the store', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByText('Definition'));
            expect(useAppStore.getState().dagViewSource).toBe('current');
        });

        it('clicking the History button only opens the picker — does not itself change dagViewSource', () => {
            // The History button keeps its existing job (open the execution
            // picker); it does not double as a one-click "back to run" action.
            // Returning to run mode happens by picking a specific execution
            // from the list (covered by setSelectedExecution's centralized
            // reset, tested in useAppStore.test.ts and PipelinesSidebar.test.tsx).
            setStore({ dagViewSource: 'current' });
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/ }));
            expect(useAppStore.getState().dagViewSource).toBe('current');
        });

        it('clicking "Definition" closes the history picker if it was open', () => {
            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByRole('button', { name: /History/ }));
            expect(screen.getByRole('dialog', { name: /Execution history/ })).toBeInTheDocument();
            fireEvent.click(screen.getByText('Definition'));
            expect(screen.queryByRole('dialog', { name: /Execution history/ })).not.toBeInTheDocument();
        });

        it('shows the current-structure banner instead of "No executions", even with real tasks loaded', () => {
            // The whole point: real tasks exist (a run happened today), but the
            // person deliberately asked for current structure — PipelineDetail
            // must ignore mockDetailQuery's tasks in this mode, not show a
            // "No executions" message that implies something's wrong.
            mockDetailQuery.data = { tasks: mockTasks, dag: mockDag, serverOffsetMs: 0, selectedExecution: null };
            setStore({ dagViewSource: 'current' });

            render(<PipelineDetail {...defaultProps} />);

            expect(screen.getByText(/Showing the current deployed structure/)).toBeInTheDocument();
            expect(screen.queryByText(/No executions for/)).not.toBeInTheDocument();
        });

        it('"View latest run" from current-structure mode resets dagViewSource', () => {
            mockPipelinesQuery.data = [createPipeline({ recent_runs: [{ date: '2024-01-10' }] })];
            setStore({ dagViewSource: 'current' });

            render(<PipelineDetail {...defaultProps} />);
            fireEvent.click(screen.getByText(/View latest run/));

            expect(useAppStore.getState().dagViewSource).toBe('run');
        });

        it('auto-defaults to "current" for a pipeline that has never run at all', () => {
            // No recent_runs at all — latestRunDate resolves to null. The
            // toggle would otherwise show "History" as active while the
            // content displayed is actually the pre-existing "no
            // executions" blueprint fallback — a mismatch between what the
            // control claims and what's on screen.
            mockPipelinesQuery.data = [createPipeline({ name: 'acme-daily' })];

            render(<PipelineDetail {...defaultProps} />);

            expect(useAppStore.getState().dagViewSource).toBe('current');
        });

        it('does not auto-default when the pipeline has actually run before', () => {
            mockPipelinesQuery.data = [createPipeline({ name: 'acme-daily', recent_runs: [{ date: '2024-01-10' }] })];

            render(<PipelineDetail {...defaultProps} />);

            expect(useAppStore.getState().dagViewSource).toBe('run');
        });

        it('does not auto-default while the pipelines list is still loading', () => {
            // usePipelinesQuery defaults to [] while loading — without the
            // pipelines.length > 0 guard, a not-yet-loaded pipeline would
            // look identical to a genuinely never-run one and incorrectly
            // flip to 'current', with no way back once real data arrives
            // (the effect only acts when latestRunDate is null).
            mockPipelinesQuery.data = [];

            render(<PipelineDetail {...defaultProps} />);

            expect(useAppStore.getState().dagViewSource).toBe('run');
        });
    });

    describe('empty-graph banner', () => {
        it('explains an empty graph even when runs exist elsewhere', () => {
            mockDetailQuery.data = { tasks: [], dag: mockDag, serverOffsetMs: 0, selectedExecution: null };
            mockExecQuery.data = mockExecList;          // the runs endpoint still has rows

            render(<PipelineDetail {...defaultProps} />);

            expect(screen.getByText(/No executions for/)).toBeInTheDocument();
        });

        it('offers a way out of the empty date', () => {
            mockDetailQuery.data = { tasks: [], dag: mockDag, serverOffsetMs: 0, selectedExecution: null };

            render(<PipelineDetail {...defaultProps} />);

            expect(screen.getByText(/Show older runs|View latest run|Execution history/)).toBeInTheDocument();
        });

        it('stays out of the way when the graph has tasks', () => {
            mockDetailQuery.data = { tasks: mockTasks, dag: mockDag, serverOffsetMs: 0, selectedExecution: null };

            render(<PipelineDetail {...defaultProps} />);

            expect(screen.queryByText(/No executions for/)).not.toBeInTheDocument();
        });
    });

});
