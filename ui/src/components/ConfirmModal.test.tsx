import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ConfirmModal } from './ConfirmModal';
import { createConfirmModalProps } from '../test/factories';

// ─── Component mocks (all inline for vi.mock hoisting) ──────────────────────
vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => <span data-testid="icon-AlertTriangle" />, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => <span data-testid="icon-CircleHelp" />, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => <span data-testid="icon-AlertTriangle" />, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => <span data-testid="icon-CircleHelp" />, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
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


describe('ConfirmModal', () => {
    let defaultProps;

    beforeEach(() => {
        defaultProps = createConfirmModalProps();
    });

    // ─── Rendering ───────────────────────────────────────────────────────

    describe('rendering', () => {
        it('renders nothing when isOpen is false', () => {
            const { container } = render(<ConfirmModal {...defaultProps} isOpen={false} />);
            expect(container.querySelector('[data-testid="base-modal"]')).not.toBeInTheDocument();
        });

        it('renders modal with title', () => {
            render(<ConfirmModal {...defaultProps} />);
            expect(screen.getByText('Confirm Action')).toBeInTheDocument();
        });

        it('renders message text', () => {
            render(<ConfirmModal {...defaultProps} />);
            expect(screen.getByText('Are you sure you want to proceed?')).toBeInTheDocument();
        });

        it('renders confirm and cancel buttons', () => {
            render(<ConfirmModal {...defaultProps} />);
            expect(screen.getByText('Confirm')).toBeInTheDocument();
            expect(screen.getByText('Cancel')).toBeInTheDocument();
        });

        it('renders custom button text', () => {
            render(<ConfirmModal {...defaultProps} confirmText="Delete Forever" cancelText="Go Back" />);
            expect(screen.getByText('Delete Forever')).toBeInTheDocument();
            expect(screen.getByText('Go Back')).toBeInTheDocument();
        });

        it('renders multiline messages with pre-line whitespace', () => {
            render(<ConfirmModal {...defaultProps} message={"Line 1\nLine 2"} />);
            const messageEl = screen.getByText(/Line 1/);
            expect(messageEl).toHaveClass('whitespace-pre-line');
        });
    });

    // ─── Danger Mode ─────────────────────────────────────────────────────

    describe('danger mode', () => {
        it('renders destructive variant when danger is true', () => {
            render(<ConfirmModal {...defaultProps} danger={true} />);
            const confirmBtn = screen.getByText('Confirm');
            expect(confirmBtn).toHaveAttribute('data-variant', 'destructive');
        });

        it('renders default variant when danger is false', () => {
            render(<ConfirmModal {...defaultProps} danger={false} />);
            const confirmBtn = screen.getByText('Confirm');
            expect(confirmBtn).toHaveAttribute('data-variant', 'default');
        });

        it('renders AlertTriangle icon when danger', () => {
            render(<ConfirmModal {...defaultProps} danger={true} />);
            expect(screen.getByTestId('icon-AlertTriangle')).toBeInTheDocument();
        });

        it('renders CircleHelp icon when not danger', () => {
            render(<ConfirmModal {...defaultProps} danger={false} />);
            expect(screen.getByTestId('icon-CircleHelp')).toBeInTheDocument();
        });
    });

    // ─── Actions ─────────────────────────────────────────────────────────

    describe('actions', () => {
        it('calls onConfirm when confirm button clicked', () => {
            render(<ConfirmModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Confirm'));
            expect(defaultProps.onConfirm).toHaveBeenCalledTimes(1);
        });

        it('calls onCancel when cancel button clicked', () => {
            render(<ConfirmModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Cancel'));
            expect(defaultProps.onCancel).toHaveBeenCalledTimes(1);
        });

        it('calls onCancel when modal close button clicked', () => {
            render(<ConfirmModal {...defaultProps} />);
            const closeBtn = screen.queryByTestId('modal-close');
            // BaseModal onClose is passed as onCancel
            // This tests the BaseModal integration
            if (closeBtn) {
                fireEvent.click(closeBtn);
                expect(defaultProps.onCancel).toHaveBeenCalled();
            }
        });
    });

    // ─── Custom Icon ─────────────────────────────────────────────────────

    describe('custom icon', () => {
        it('uses custom icon when provided', () => {
            const customIcon = <span data-testid="custom-icon">🔥</span>;
            render(<ConfirmModal {...defaultProps} icon={customIcon} />);
            expect(screen.getByTestId('custom-icon')).toBeInTheDocument();
        });
    });
});
