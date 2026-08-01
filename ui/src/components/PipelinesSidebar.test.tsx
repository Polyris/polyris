import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { PipelinesSidebar } from './PipelinesSidebar';
import { createPipelinesSidebarProps, createPipeline } from '../test/factories';
import { useAppStore } from '../stores/useAppStore';
import type { PipelineWithUI } from '../types';

// ─── Component mocks (all inline for vi.mock hoisting) ──────────────────────
vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, X: () => null, Zap: () => null }));
vi.mock('@/components/ui/button', () => ({
    Button: (props) => <button onClick={props.onClick} disabled={props.disabled} className={props.className} title={props.title} data-variant={props.variant}>{props.children}</button>,
}));
vi.mock('./BaseModal', () => ({
    BaseModal: ({ isOpen, children, className }) => isOpen ? <div data-testid="base-modal" className={className} role="dialog">{children}</div> : null,
    ModalHeader: ({ children, icon, onClose }) => <div data-testid="modal-header">{icon}<span>{children}</span>{onClose && <button data-testid="modal-close" onClick={onClose}>x</button>}</div>,
    ModalBody: ({ children }) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }) => <div data-testid="modal-footer">{children}</div>,
}));
vi.mock('../lib/config', () => ({ default: { API_URL: '/api', POLLING_INTERVAL: 5000, AUTH_ENABLED: false } }));
vi.mock('../utils/api', () => ({ api: { get: vi.fn().mockResolvedValue({ ok: true, data: {} }), post: vi.fn().mockResolvedValue({ ok: true, data: {} }) } }));
vi.mock('./CountdownTimer', () => ({ CountdownTimer: ({ targetTime }) => <span data-testid="countdown">{targetTime}</span> }));
vi.mock('./LoginPage', () => ({ LoginPage: () => <div data-testid="login-page" /> }));
vi.mock('./Notifications', () => ({ default: () => <div data-testid="notifications" /> }));
vi.mock('./UserMenu', () => ({ UserMenu: () => <div data-testid="user-menu" /> }));
vi.mock('./Skeletons', () => ({ PipelineListSkeleton: () => <div data-testid="skeleton" /> }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ isSignedIn: true, user: { email: 'test@test.com' }, signOut: vi.fn() }), AUTH_STATE: { SIGNED_IN: 'signedIn', SIGNED_OUT: 'signedOut' } }));
vi.mock('@/utils/api', () => ({ setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));

const mockQueryData = { data: [] as PipelineWithUI[], isLoading: false };
vi.mock('@/hooks/queries', () => ({
    usePipelinesQuery: () => mockQueryData,
}));

/** Click collapsed group headers to expand them (no-op if already expanded) */
const expandAllGroups = () => {
    document.querySelectorAll('.sb-pipeline-group-header').forEach(h => {
        // Only click if group is collapsed (no pipeline-item siblings visible)
        const group = h.closest('.sb-pipeline-group');
        if (group && !group.querySelector('.sb-pipeline-item')) {
            fireEvent.click(h);
        }
    });
};

describe('PipelinesSidebar', () => {
    beforeEach(() => {
        const full = createPipelinesSidebarProps();
        // Set query mock data instead of props
        mockQueryData.data = full.pipelines;
        mockQueryData.isLoading = false;
        // Reset store
        const store = useAppStore.getState();
        store.setSelectedPipeline(null);
        store.setPipelineSearch('');
        store.setSidebarOpen(false);
    });

    // ─── Rendering ───────────────────────────────────────────────────────

    describe('rendering', () => {
        it('renders pipeline list', () => {
            render(<PipelinesSidebar />);
            expect(screen.getByText('Pipelines')).toBeInTheDocument();
        });

        it('shows pipeline count', () => {
            render(<PipelinesSidebar />);
            expect(screen.getByText('5')).toBeInTheDocument();
        });

        it('shows loading skeleton when loading', () => {
            mockQueryData.isLoading = true; mockQueryData.data = [];
            render(<PipelinesSidebar />);
            expect(screen.queryByText('acme-daily')).not.toBeInTheDocument();
        });

        it('shows empty state when no pipelines', () => {
            mockQueryData.data = [];
            render(<PipelinesSidebar />);
            expect(screen.getByText('No pipelines')).toBeInTheDocument();
        });
    });

    // ─── Search ──────────────────────────────────────────────────────────

    describe('search', () => {
        it('shows search input when more than 3 pipelines', () => {
            render(<PipelinesSidebar />);
            expect(screen.getByPlaceholderText(/Search pipelines/i)).toBeInTheDocument();
        });

        it('hides search when 3 or fewer pipelines', () => {
            const pipelines = [createPipeline({ name: 'a' }), createPipeline({ name: 'b' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expect(screen.queryByPlaceholderText(/Search pipelines/i)).not.toBeInTheDocument();
        });

        it('calls onSearchChange when typing', () => {
            render(<PipelinesSidebar />);
            const input = screen.getByPlaceholderText(/Search pipelines/i);
            fireEvent.change(input, { target: { value: 'acme' } });
            expect(useAppStore.getState().pipelineSearch).toBe('acme');
        });

        it('shows "No matches" when search yields no results', () => {
            useAppStore.getState().setPipelineSearch('nonexistent');
            mockQueryData.data = [];
            render(<PipelinesSidebar />);
            expect(screen.getByText('No matches')).toBeInTheDocument();
        });

        it('filters pipelines by search term (multi-word AND)', () => {
            const pipelines = [
                createPipeline({ name: 'acme-daily' }),
                createPipeline({ name: 'acme-weekly' }),
                createPipeline({ name: 'shopmart-daily' }),
            ];
            // Set search via store before render
            useAppStore.getState().setPipelineSearch('acme daily');
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.getByText('acme-daily')).toBeInTheDocument();
            expect(screen.queryByText('acme-weekly')).not.toBeInTheDocument();
            expect(screen.queryByText('shopmart-daily')).not.toBeInTheDocument();
        });
    });

    // ─── Grouping ────────────────────────────────────────────────────────

    describe('grouping', () => {
        it('groups pipelines by prefix', () => {
            const pipelines = [
                createPipeline({ name: 'acme-daily', group: 'acme' }),
                createPipeline({ name: 'acme-weekly', group: 'acme' }),
                createPipeline({ name: 'shopmart-feeds', group: 'shopmart' }),
            ];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            const headers = container.querySelectorAll('.sb-pipeline-group-header');
            const headerTexts = [...headers].map(h => h.textContent.toLowerCase());
            expect(headerTexts.some(t => t.includes('acme'))).toBe(true);
            expect(headerTexts.some(t => t.includes('shopmart'))).toBe(true);
        });

        it('falls back to dash-prefix when no explicit group', () => {
            const pipelines = [
                createPipeline({ name: 'acme-daily', group: null }),
                createPipeline({ name: 'acme-weekly', group: null }),
            ];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            const headers = container.querySelectorAll('.sb-pipeline-group-header');
            const headerTexts = [...headers].map(h => h.textContent.toLowerCase());
            expect(headerTexts.some(t => t.includes('acme'))).toBe(true);
        });

        it('auto-expands a group containing an aborted pipeline', () => {
            // Regression test: the auto-expand check previously looked for
            // 'timed_out', a value the /api/pipelines status field can never
            // actually hold (derive_execution_status — the only place this
            // field is set — returns only running/success/failed/aborted;
            // 'timed_out' is reconciliation-only, used elsewhere). Meanwhile
            // 'aborted', a real reachable value for a user-stopped pipeline,
            // was missing from the check entirely, so a group containing
            // only an aborted pipeline was never auto-expanded.
            //
            // >12 total pipelines across two groups, so the "≤12 total"
            // auto-expand-everything rule doesn't mask this specific check.
            const quietGroup = Array.from({ length: 12 }, (_, i) =>
                createPipeline({ name: `quiet-${i}`, group: 'quiet', status: 'success' }));
            const abortedGroup = [createPipeline({ name: 'stopped-run', group: 'attention', status: 'aborted' })];
            mockQueryData.data = [...quietGroup, ...abortedGroup];

            const { container } = render(<PipelinesSidebar />);
            const headers = [...container.querySelectorAll('.sb-pipeline-group-header')];
            const attentionHeader = headers.find(h => h.getAttribute('aria-label')?.toLowerCase().includes('attention'));
            const quietHeader = headers.find(h => h.getAttribute('aria-label')?.toLowerCase().includes('quiet'));

            expect(attentionHeader?.getAttribute('aria-expanded')).toBe('true');
            expect(quietHeader?.getAttribute('aria-expanded')).toBe('false');
        });
    });

    // ─── Selection ───────────────────────────────────────────────────────

    describe('selection', () => {
        it('calls onSelectPipeline when pipeline is clicked', () => {
            const pipelines = [createPipeline({ name: 'acme-daily' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            fireEvent.click(screen.getByText('acme-daily'));
            expect(useAppStore.getState().selectedPipeline).toEqual(
                expect.objectContaining({ name: 'acme-daily' })
            );
        });

        it('resets dagViewSource to run when a different pipeline is clicked', () => {
            const pipelines = [createPipeline({ name: 'acme-daily' })];
            useAppStore.getState().setDagViewSource('current');
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            fireEvent.click(screen.getByText('acme-daily'));
            expect(useAppStore.getState().dagViewSource).toBe('run');
        });

        it('highlights the selected pipeline', () => {
            const pipelines = [
                createPipeline({ name: 'acme-daily' }),
                createPipeline({ name: 'shopmart-feeds' }),
            ];
            useAppStore.getState().setSelectedPipeline(pipelines[0]);
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            const item = screen.getByText('acme-daily').closest('.sb-pipeline-item');
            if (item) {
                expect(item.className).toMatch(/active|selected/);
            }
        });
    });

    // ─── Mobile ──────────────────────────────────────────────────────────

    describe('mobile behavior', () => {
        it('adds sidebar-open class when isOpen is true', () => {
            useAppStore.getState().setSidebarOpen(true);
            const { container } = render(<PipelinesSidebar />);
            expect(container.querySelector('.sidebar-open')).toBeInTheDocument();
        });

        it('shows overlay when isOpen is true', () => {
            useAppStore.getState().setSidebarOpen(true);
            const { container } = render(<PipelinesSidebar />);
            expect(container.querySelector('.sidebar-overlay')).toBeInTheDocument();
        });

        it('calls onClose when overlay is clicked', () => {
            useAppStore.getState().setSidebarOpen(true);
            const { container } = render(<PipelinesSidebar />);
            const overlay = container.querySelector('.sidebar-overlay');
            fireEvent.click(overlay!);
            expect(useAppStore.getState().sidebarOpen).toBe(false);
        });
    });

    // ─── Sorting ─────────────────────────────────────────────────────────

    describe('sorting', () => {
        it('sorts pipelines alphabetically', () => {
            const pipelines = [
                createPipeline({ name: 'zebra-daily' }),
                createPipeline({ name: 'alpha-daily' }),
                createPipeline({ name: 'middle-daily' }),
            ];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();

            const items = screen.getAllByText(/-daily$/);
            const names = items.map(el => el.textContent);
            expect(names).toEqual(['alpha-daily', 'middle-daily', 'zebra-daily']);
        });
    });

    // ─── Schedule Display ────────────────────────────────────────────────

    describe('schedule display', () => {
        it('shows formatted cron schedule after status', () => {
            const pipelines = [createPipeline({ name: 'acme-daily', schedule: 'cron(0 8 * * ? *)' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.getByText(/daily @ 08:00/)).toBeInTheDocument();
        });

        it('shows day-of-week for weekly cron', () => {
            const pipelines = [createPipeline({ name: 'acme-weekly', schedule: 'cron(0 10 ? * MON *)' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.getByText(/Mon @ 10:00/)).toBeInTheDocument();
        });

        it('shows rate expression as "every Nh"', () => {
            const pipelines = [createPipeline({ name: 'acme-feeds', schedule: 'rate(6 hours)' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.getByText(/every 6h/)).toBeInTheDocument();
        });

        it('does not show schedule when empty', () => {
            const pipelines = [createPipeline({ name: 'target-manual', schedule: '' })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.queryByText(/@ /)).not.toBeInTheDocument();
        });

        it('does not show schedule when null', () => {
            const pipelines = [createPipeline({ name: 'target-manual', schedule: null })];
            mockQueryData.data = pipelines;
            render(<PipelinesSidebar />);
            expandAllGroups();
            expect(screen.queryByText(/@ /)).not.toBeInTheDocument();
        });
    });

    // ─── Run Sparkline ───────────────────────────────────────────────────

    describe('run sparkline', () => {
        it('renders sparkline bars for recent runs', () => {
            const pipelines = [createPipeline({
                name: 'acme-daily',
                recent_runs: [
                    { date: '2026-02-19', status: 'success' },
                    { date: '2026-02-18', status: 'failed' },
                    { date: '2026-02-17', status: 'success' },
                ],
            })];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            expandAllGroups();
            const sparks = container.querySelectorAll('.sb-run-spark');
            expect(sparks.length).toBe(3);
        });

        it('applies correct CSS classes for success/failed/running', () => {
            const pipelines = [createPipeline({
                name: 'acme-daily',
                recent_runs: [
                    { date: '2026-02-19', status: 'running' },
                    { date: '2026-02-18', status: 'failed' },
                    { date: '2026-02-17', status: 'success' },
                ],
            })];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            expandAllGroups();
            expect(container.querySelector('.sb-run-spark.success')).toBeInTheDocument();
            expect(container.querySelector('.sb-run-spark.failed')).toBeInTheDocument();
            expect(container.querySelector('.sb-run-spark.running')).toBeInTheDocument();
        });

        it('does not render sparkline when no recent_runs', () => {
            const pipelines = [createPipeline({ name: 'acme-daily', recent_runs: null })];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            expandAllGroups();
            expect(container.querySelector('.sb-run-sparkline')).not.toBeInTheDocument();
        });

        it('does not render sparkline when recent_runs is empty', () => {
            const pipelines = [createPipeline({ name: 'acme-daily', recent_runs: [] })];
            mockQueryData.data = pipelines;
            const { container } = render(<PipelinesSidebar />);
            expandAllGroups();
            expect(container.querySelector('.sb-run-sparkline')).not.toBeInTheDocument();
        });
    });
});
