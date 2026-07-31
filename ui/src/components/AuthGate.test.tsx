import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AuthGate } from './AuthGate';

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
vi.mock('@/hooks/useAuth', () => { const m = vi.fn(); return { useAuth: m, AUTH_STATE: { SIGNED_IN: 'signedIn', SIGNED_OUT: 'signedOut', LOADING: 'loading' }, __mockUseAuth: m }; });
vi.mock('@/utils/api', () => { const g = vi.fn(); const e = vi.fn(); return { setAuthTokenGetter: g, setAuthErrorCallback: e, __mockSetAuthTokenGetter: g, __mockSetAuthErrorCallback: e }; });


// Get mock references
import { useAuth as mockUseAuth } from '@/hooks/useAuth';
import { setAuthTokenGetter as mockSetAuthTokenGetter, setAuthErrorCallback as mockSetAuthErrorCallback } from '@/utils/api';

describe('AuthGate', () => {
    const createAuthState = (overrides = {}) => ({
        authState: 'authenticated',
        isAuthenticated: true,
        isLoading: false,
        isAuthEnabled: true,
        getAccessToken: vi.fn().mockResolvedValue('test-token'),
        signOut: vi.fn(),
        ...overrides,
    });

    beforeEach(() => {
        mockUseAuth.mockReturnValue(createAuthState());
        mockSetAuthTokenGetter.mockClear();
        mockSetAuthErrorCallback.mockClear();
    });

    // ─── Auth Disabled ───────────────────────────────────────────────────

    describe('auth disabled', () => {
        it('renders children directly when auth is disabled', () => {
            mockUseAuth.mockReturnValue(createAuthState({
                isAuthEnabled: false,
                isAuthenticated: false,
            }));
            render(
                <AuthGate>
                    <div data-testid="app-content">App</div>
                </AuthGate>
            );
            expect(screen.getByTestId('app-content')).toBeInTheDocument();
        });

        it('does not set auth token getter when auth disabled', () => {
            mockUseAuth.mockReturnValue(createAuthState({ isAuthEnabled: false }));
            render(<AuthGate><div>App</div></AuthGate>);
            expect(mockSetAuthTokenGetter).not.toHaveBeenCalledWith(expect.any(Function));
        });
    });

    // ─── Loading State ───────────────────────────────────────────────────

    describe('loading state', () => {
        it('renders loading UI during auth initialization', () => {
            mockUseAuth.mockReturnValue(createAuthState({
                isLoading: true,
                isAuthenticated: false,
            }));
            render(<AuthGate><div data-testid="app-content">App</div></AuthGate>);
            
            expect(screen.getByText('Loading...')).toBeInTheDocument();
            expect(screen.queryByTestId('app-content')).not.toBeInTheDocument();
        });
    });

    // ─── Unauthenticated ─────────────────────────────────────────────────

    describe('unauthenticated', () => {
        it('renders login page when not authenticated', () => {
            mockUseAuth.mockReturnValue(createAuthState({
                isAuthenticated: false,
                isLoading: false,
            }));
            render(<AuthGate><div data-testid="app-content">App</div></AuthGate>);
            
            expect(screen.getByTestId('login-page')).toBeInTheDocument();
            expect(screen.queryByTestId('app-content')).not.toBeInTheDocument();
        });
    });

    // ─── Authenticated ───────────────────────────────────────────────────

    describe('authenticated', () => {
        it('renders children when authenticated', () => {
            render(
                <AuthGate>
                    <div data-testid="app-content">App Content</div>
                </AuthGate>
            );
            expect(screen.getByTestId('app-content')).toBeInTheDocument();
            expect(screen.queryByTestId('login-page')).not.toBeInTheDocument();
        });

        it('sets auth token getter for API requests', () => {
            const getAccessToken = vi.fn();
            mockUseAuth.mockReturnValue(createAuthState({ getAccessToken }));
            render(<AuthGate><div>App</div></AuthGate>);
            
            expect(mockSetAuthTokenGetter).toHaveBeenCalledWith(getAccessToken);
        });

        it('sets auth error callback for 401 handling', () => {
            mockUseAuth.mockReturnValue(createAuthState());
            render(<AuthGate><div>App</div></AuthGate>);
            
            expect(mockSetAuthErrorCallback).toHaveBeenCalledWith(expect.any(Function));
        });

        it('cleans up token getter and error callback on unmount', () => {
            const { unmount } = render(<AuthGate><div>App</div></AuthGate>);
            unmount();
            
            expect(mockSetAuthTokenGetter).toHaveBeenCalledWith(null);
            expect(mockSetAuthErrorCallback).toHaveBeenCalledWith(null);
        });
    });
});
