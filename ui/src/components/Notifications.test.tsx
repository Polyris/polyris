import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import Notifications from './Notifications';
import { createNotification } from '../test/factories';

// ─── Component mocks (all inline for vi.mock hoisting) ──────────────────────
vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellOff: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellOff: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, X: () => null, Zap: () => null }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ user: { username: 'test-user' }, isAuthEnabled: true, signOut: vi.fn() }) }));
vi.mock('@/components/ui/button', () => ({
    Button: (props) => <button onClick={props.onClick} disabled={props.disabled} className={props.className} title={props.title} data-variant={props.variant}>{props.children}</button>,
}));
vi.mock('./BaseModal', () => ({
    BaseModal: ({ isOpen, children, className }) => isOpen ? <div data-testid="base-modal" className={className} role="dialog">{children}</div> : null,
    ModalHeader: ({ children, icon, onClose }) => <div data-testid="modal-header">{icon}<span>{children}</span>{onClose && <button data-testid="modal-close" onClick={onClose}>x</button>}</div>,
    ModalBody: ({ children }) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }) => <div data-testid="modal-footer">{children}</div>,
}));

// Mock useNotificationsQuery hook
const { mockQueryData } = vi.hoisted(() => ({
    mockQueryData: { current: [] as unknown[] },
}));
vi.mock('@/hooks/queries', () => ({
    useNotificationsQuery: () => ({
        data: mockQueryData.current,
        isLoading: false,
        error: null,
    }),
}));


describe('Notifications', () => {
    let onNavigate;
    
    beforeEach(() => {
        onNavigate = vi.fn();
        mockQueryData.current = [];
        
        // Mock Notification API
        Object.defineProperty(window, 'Notification', {
            value: {
                permission: 'denied',
                requestPermission: vi.fn().mockResolvedValue('denied'),
            },
            writable: true,
            configurable: true,
        });
    });

    // ─── Bell Icon ───────────────────────────────────────────────────────

    describe('bell icon', () => {
        it('renders bell button', () => {
            render(<Notifications onNavigate={onNavigate} />);
            const bellBtn = screen.getByTitle('No notifications');
            expect(bellBtn).toBeInTheDocument();
        });

        it('shows badge with notification count', () => {
            mockQueryData.current = [createNotification({ id: 'n1' }), createNotification({ id: 'n2' })];
            render(<Notifications onNavigate={onNavigate} />);
            expect(screen.getByText('2')).toBeInTheDocument();
        });

        it('shows 9+ for more than 9 notifications', () => {
            mockQueryData.current = Array.from({ length: 10 }, (_, i) => createNotification({ id: `n${i}` }));
            render(<Notifications onNavigate={onNavigate} />);
            expect(screen.getByText('9+')).toBeInTheDocument();
        });
    });

    // ─── Dropdown ────────────────────────────────────────────────────────

    describe('dropdown', () => {
        it('toggles dropdown on bell click', () => {
            mockQueryData.current = [createNotification({ id: 'n1' })];
            render(<Notifications onNavigate={onNavigate} />);

            // Open dropdown
            fireEvent.click(screen.getByTitle('1 notifications'));
            expect(screen.getByText('Notifications')).toBeInTheDocument();
        });

        it('shows empty state when no notifications', () => {
            render(<Notifications onNavigate={onNavigate} />);
            fireEvent.click(screen.getByTitle('No notifications'));
            expect(screen.getByText('No new notifications')).toBeInTheDocument();
        });

        it('shows notification items in dropdown', () => {
            mockQueryData.current = [createNotification({ id: 'n1', pipeline_name: 'acme-daily', task_name: 'extract', time_ago: '3m ago' })];
            render(<Notifications onNavigate={onNavigate} />);

            fireEvent.click(screen.getByTitle('1 notifications'));
            expect(screen.getByText('acme-daily')).toBeInTheDocument();
            expect(screen.getByText(/extract.*3m ago/)).toBeInTheDocument();
        });
    });

    // ─── Dismiss ─────────────────────────────────────────────────────────

    describe('dismiss', () => {
        it('marks a notification as read (badge drops, item lingers dimmed)', async () => {
            mockQueryData.current = [
                createNotification({ id: 'n1', pipeline_name: 'pipeline-a' }),
                createNotification({ id: 'n2', pipeline_name: 'pipeline-b' }),
            ];
            render(<Notifications onNavigate={onNavigate} />);

            // Open dropdown
            fireEvent.click(screen.getByTitle('2 notifications'));
            
            // Acknowledge (mark read) the first notification
            const readBtns = screen.getAllByTitle('Mark as read');
            fireEvent.click(readBtns[0]);
            
            // Badge drops to unread count (1), but the item stays visible (lingering).
            await waitFor(() => {
                expect(screen.getByText('1')).toBeInTheDocument();
            });
            expect(screen.getByText('pipeline-a')).toBeInTheDocument();
        });

        it('dismisses all notifications via Clear All', async () => {
            mockQueryData.current = [createNotification({ id: 'n1' }), createNotification({ id: 'n2' })];
            render(<Notifications onNavigate={onNavigate} />);

            fireEvent.click(screen.getByTitle('2 notifications'));
            fireEvent.click(screen.getByText('Clear All'));

            // All cleared
            await waitFor(() => {
                expect(screen.queryByText('1')).not.toBeInTheDocument();
                expect(screen.queryByText('2')).not.toBeInTheDocument();
            });
        });

        it('auto-expires a read notification after the linger window', async () => {
            vi.useFakeTimers();
            try {
                mockQueryData.current = [createNotification({ id: 'n1', pipeline_name: 'pipeline-a' })];
                render(<Notifications onNavigate={onNavigate} />);
                fireEvent.click(screen.getByTitle('1 notifications'));
                fireEvent.click(screen.getByTitle('Mark as read'));
                // Still visible right after acknowledging.
                expect(screen.getByText('pipeline-a')).toBeInTheDocument();
                // Advance past the 48-hour linger window; the read item auto-expires.
                await act(async () => { vi.advanceTimersByTime(49 * 60 * 60 * 1000); });
                expect(screen.queryByText('pipeline-a')).not.toBeInTheDocument();
            } finally {
                vi.useRealTimers();
            }
        });
    });

    // ─── Navigation ──────────────────────────────────────────────────────

    describe('navigation', () => {
        it('calls onNavigate when View is clicked on toast', () => {
            mockQueryData.current = [createNotification({ 
                id: 'n1', 
                pipeline_name: 'acme-daily',
                pipeline_execution: 'exec-123',
            })];
            render(<Notifications onNavigate={onNavigate} />);

            // Toast should show
            const viewBtns = screen.getAllByText('View');
            fireEvent.click(viewBtns[0]);

            expect(onNavigate).toHaveBeenCalledWith('acme-daily', 'exec-123');
        });
    });

    // ─── Data Source ─────────────────────────────────────────────────────

    describe('data source', () => {
        it('uses React Query hook for data fetching', () => {
            // Verify component renders with hook data
            mockQueryData.current = [createNotification({ id: 'n1', pipeline_name: 'hook-test' })];
            render(<Notifications onNavigate={onNavigate} />);
            expect(screen.getByText('1')).toBeInTheDocument();
        });

        it('renders correctly when hook returns empty data', () => {
            mockQueryData.current = [];
            render(<Notifications onNavigate={onNavigate} />);
            expect(screen.getByTitle('No notifications')).toBeInTheDocument();
        });
    });

    // ─── Toast Notifications ─────────────────────────────────────────────

    describe('toast notifications', () => {
        it('renders toasts when notifications present and dropdown closed', () => {
            mockQueryData.current = [createNotification({ id: 'n1', pipeline_name: 'acme-daily' })];
            render(<Notifications onNavigate={onNavigate} />);
            expect(screen.getByText('Pipeline Failed')).toBeInTheDocument();
        });

        it('limits toasts to 3', () => {
            mockQueryData.current = Array.from({ length: 5 }, (_, i) => 
                createNotification({ id: `n${i}`, pipeline_name: `pipeline-${i}` })
            );
            render(<Notifications onNavigate={onNavigate} />);
            const toasts = screen.getAllByText('Pipeline Failed');
            expect(toasts.length).toBe(3);
        });
    });

    describe('decision-required notifications', () => {
        it('renders a decision-required notification with awaiting-decision text', () => {
            mockQueryData.current = [createNotification({
                id: 'd1', type: 'decision_required', pipeline_name: 'pipeline-x', task_name: 'approve',
            })];
            render(<Notifications onNavigate={onNavigate} />);
            fireEvent.click(screen.getByTitle('1 notifications'));
            expect(screen.getByText('pipeline-x')).toBeInTheDocument();
            expect(screen.getByText(/awaiting decision/i)).toBeInTheDocument();
        });
    });


    describe('enable/disable toggle', () => {
        it('mutes notifications, hides the badge, and persists per user', () => {
            localStorage.removeItem('polyris-notifications-enabled:test-user');
            mockQueryData.current = [createNotification({ id: 'n1' })];
            render(<Notifications onNavigate={onNavigate} />);
            // badge visible while enabled
            fireEvent.click(screen.getByTitle('1 notifications'));
            expect(screen.getByText('1')).toBeInTheDocument();
            // toggle off
            fireEvent.click(screen.getByTitle('Turn notifications off'));
            expect(localStorage.getItem('polyris-notifications-enabled:test-user')).toBe('false');
            // bell now muted, badge gone
            expect(screen.getByTitle('Notifications off')).toBeInTheDocument();
            expect(screen.queryByText('1')).not.toBeInTheDocument();
        });
    });


    describe('floating toasts', () => {
        it('does not keep floating a read notification', () => {
            localStorage.removeItem('polyris-read-notifications:test-user');
            mockQueryData.current = [createNotification({ id: 't1', pipeline_name: 'toast-pipe' })];
            render(<Notifications onNavigate={onNavigate} />);
            // Dropdown is closed by default -> the unread notification floats as a toast.
            expect(screen.getByText(/toast-pipe/)).toBeInTheDocument();
            // Acknowledge via the toast's "Mark read" button.
            fireEvent.click(screen.getByText('Mark read'));
            // Now read -> it lingers in the dropdown but must stop floating.
            expect(screen.queryByText(/toast-pipe/)).not.toBeInTheDocument();
        });
    });

});
