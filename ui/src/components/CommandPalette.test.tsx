import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { CommandPalette } from './CommandPalette';
import { createCommandPaletteProps, createPipeline } from '../test/factories';

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
const mockInvalidateQueries = vi.fn();
vi.mock('@tanstack/react-query', () => ({
    useQueryClient: () => ({ invalidateQueries: mockInvalidateQueries }),
}));


describe('CommandPalette', () => {
    let defaultProps;

    beforeEach(() => {
        mockInvalidateQueries.mockClear();
        defaultProps = {
            ...createCommandPaletteProps(),
            onNavigate: vi.fn(),
            onToggleTheme: vi.fn(),
            theme: 'light',
        };
    });

    // ─── Rendering ───────────────────────────────────────────────────────

    describe('rendering', () => {
        it('renders nothing when isOpen is false', () => {
            render(<CommandPalette {...defaultProps} isOpen={false} />);
            // Should not show the search input
            expect(screen.queryByPlaceholderText(/search|type/i)).not.toBeInTheDocument();
        });

        it('renders search input when open', () => {
            render(<CommandPalette {...defaultProps} />);
            expect(screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i)).toBeInTheDocument();
        });

        it('shows default commands when no query', () => {
            render(<CommandPalette {...defaultProps} />);
            expect(screen.getByText('Commands')).toBeInTheDocument();
            expect(screen.getByText('Go to Pipelines')).toBeInTheDocument();
            expect(screen.getByText('Go to Assets')).toBeInTheDocument();
        });

        it('shows pipelines section', () => {
            render(<CommandPalette {...defaultProps} />);
            expect(screen.getByText('Pipelines')).toBeInTheDocument();
        });
    });

    // ─── Search / Filtering ──────────────────────────────────────────────

    describe('search filtering', () => {
        it('filters commands by query', () => {
            render(<CommandPalette {...defaultProps} />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'assets' } });
            expect(screen.getByText('Go to Assets')).toBeInTheDocument();
            expect(screen.queryByText('Go to Pipelines')).not.toBeInTheDocument();
        });

        it('filters pipelines by name', () => {
            const pipelines = [
                createPipeline({ name: 'acme-daily' }),
                createPipeline({ name: 'shopmart-weekly' }),
            ];
            render(<CommandPalette {...defaultProps} pipelines={pipelines} />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'acme' } });
            expect(screen.getByText('acme-daily')).toBeInTheDocument();
            expect(screen.queryByText('shopmart-weekly')).not.toBeInTheDocument();
        });

        it('shows no results message for unmatched query', () => {
            render(<CommandPalette {...defaultProps} pipelines={[]} />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'zzz_nonexistent_zzz' } });
            expect(screen.getByText(/No results/i)).toBeInTheDocument();
        });

        it('shows theme toggle command', () => {
            render(<CommandPalette {...defaultProps} theme="light" />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'dark' } });
            expect(screen.getByText(/Switch to Dark Mode/i)).toBeInTheDocument();
        });
    });

    // ─── Keyboard Navigation ─────────────────────────────────────────────

    describe('keyboard navigation', () => {
        it('closes on Escape', () => {
            render(<CommandPalette {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'Escape' });
            expect(defaultProps.onClose).toHaveBeenCalled();
        });

        it('executes selected item on Enter', () => {
            render(<CommandPalette {...defaultProps} />);
            // First selectable item should be "Go to Pipelines" command
            fireEvent.keyDown(document, { key: 'Enter' });
            // Should have triggered an action
            expect(defaultProps.onNavigate).toHaveBeenCalledWith('pipelines');
        });

        it('navigates down with ArrowDown', () => {
            render(<CommandPalette {...defaultProps} />);
            // Move to second item
            fireEvent.keyDown(document, { key: 'ArrowDown' });
            fireEvent.keyDown(document, { key: 'Enter' });
            // Second command is "Go to Assets"
            expect(defaultProps.onNavigate).toHaveBeenCalledWith('assets');
        });

        it('does not go below last item', () => {
            render(<CommandPalette {...defaultProps} pipelines={[]} />);
            // Press down many times
            for (let i = 0; i < 20; i++) {
                fireEvent.keyDown(document, { key: 'ArrowDown' });
            }
            // Should not crash, Enter should still work
            fireEvent.keyDown(document, { key: 'Enter' });
            // Should have called some action (the last selectable item)
            expect(defaultProps.onNavigate).toHaveBeenCalled();
        });

        it('navigates up with ArrowUp', () => {
            render(<CommandPalette {...defaultProps} />);
            // Go down then up
            fireEvent.keyDown(document, { key: 'ArrowDown' });
            fireEvent.keyDown(document, { key: 'ArrowDown' });
            fireEvent.keyDown(document, { key: 'ArrowUp' });
            fireEvent.keyDown(document, { key: 'Enter' });
            // Should be back at second item (Go to Assets)
            expect(defaultProps.onNavigate).toHaveBeenCalledWith('assets');
        });
    });

    // ─── Actions ─────────────────────────────────────────────────────────

    describe('actions', () => {
        it('selects pipeline and closes on click', () => {
            const pipeline = createPipeline({ name: 'acme-daily' });
            render(<CommandPalette {...defaultProps} pipelines={[pipeline]} />);
            fireEvent.click(screen.getByText('acme-daily'));
            expect(defaultProps.onSelectPipeline).toHaveBeenCalledWith(
                expect.objectContaining({ name: 'acme-daily' })
            );
        });

        it('"Refresh Data" actually invalidates queries, not just closes the palette', () => {
            // Regression test for a real bug: this command's action previously
            // only called onClose(), doing nothing else — clicking a button
            // labeled "Refresh Data" (or pressing its shortcut) silently
            // refreshed nothing.
            render(<CommandPalette {...defaultProps} />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'refresh' } });
            fireEvent.click(screen.getByText('Refresh Data'));
            expect(mockInvalidateQueries).toHaveBeenCalledTimes(1);
            expect(defaultProps.onClose).toHaveBeenCalled();
        });

        it('resets query when reopened', () => {
            const { rerender } = render(<CommandPalette {...defaultProps} />);
            const input = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            fireEvent.change(input, { target: { value: 'something' } });
            
            // Close and reopen
            rerender(<CommandPalette {...defaultProps} isOpen={false} />);
            rerender(<CommandPalette {...defaultProps} isOpen={true} />);
            
            const newInput = screen.getByRole('combobox') || screen.getByPlaceholderText(/search|type|command/i);
            expect(newInput.value).toBe('');
        });
    });
});
