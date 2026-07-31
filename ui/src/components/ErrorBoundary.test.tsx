import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ErrorBoundary, withErrorBoundary } from './ErrorBoundary';

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

const originalConsoleError = console.error;

// Helper component that throws when shouldThrow is true (default)
const ThrowingComponent = ({ shouldThrow = true }) => {
    if (shouldThrow) throw new Error('Test error');
    return <div data-testid="child-content">Child content</div>;
};

describe('ErrorBoundary', () => {
    beforeEach(() => {
        console.error = vi.fn();
    });

    afterAll(() => {
        console.error = originalConsoleError;
    });

    // ─── Normal Rendering ────────────────────────────────────────────────

    describe('normal rendering', () => {
        it('renders children when no error', () => {
            render(
                <ErrorBoundary>
                    <ThrowingComponent shouldThrow={false} />
                </ErrorBoundary>
            );
            expect(screen.getByTestId('child-content')).toBeInTheDocument();
        });
    });

    // ─── Error Catching ──────────────────────────────────────────────────

    describe('error catching', () => {
        it('renders error UI when child throws', () => {
            render(
                <ErrorBoundary>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(screen.getByText('Something went wrong')).toBeInTheDocument();
            expect(screen.getByText('Test error')).toBeInTheDocument();
        });

        it('renders Try Again and Reload buttons', () => {
            render(
                <ErrorBoundary>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(screen.getByText('Try Again')).toBeInTheDocument();
            expect(screen.getByText('Reload Page')).toBeInTheDocument();
        });

        it('shows technical details in collapsed section', () => {
            render(
                <ErrorBoundary>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(screen.getByText('Technical details')).toBeInTheDocument();
        });
    });

    // ─── Recovery ────────────────────────────────────────────────────────

    describe('recovery', () => {
        it('resets error state when Try Again is clicked', () => {
            // Use a ref to control throwing behavior
            let throwError = true;
            const ControlledComponent = () => {
                if (throwError) throw new Error('Recoverable error');
                return <div data-testid="recovered">Recovered!</div>;
            };

            render(
                <ErrorBoundary>
                    <ControlledComponent />
                </ErrorBoundary>
            );

            expect(screen.getByText('Something went wrong')).toBeInTheDocument();

            // Stop throwing before clicking Try Again
            throwError = false;
            fireEvent.click(screen.getByText('Try Again'));
            
            expect(screen.getByTestId('recovered')).toBeInTheDocument();
        });

        it('calls window.location.reload when Reload Page is clicked', () => {
            const reloadMock = vi.fn();
            Object.defineProperty(window, 'location', {
                value: { reload: reloadMock },
                writable: true,
            });

            render(
                <ErrorBoundary>
                    <ThrowingComponent />
                </ErrorBoundary>
            );

            fireEvent.click(screen.getByText('Reload Page'));
            expect(reloadMock).toHaveBeenCalled();
        });
    });

    // ─── Error Count ─────────────────────────────────────────────────────

    describe('error counting', () => {
        it('shows error count hint after repeated errors', () => {
            let throwCount = 0;
            const RepeatThrower = () => {
                throwCount++;
                throw new Error(`Error #${throwCount}`);
            };

            render(
                <ErrorBoundary>
                    <RepeatThrower />
                </ErrorBoundary>
            );

            // First error - no count hint yet (errorCount = 1)
            expect(screen.queryByText(/occurred.*times/)).not.toBeInTheDocument();

            // Reset and trigger again
            fireEvent.click(screen.getByText('Try Again'));

            // Second error - should show count
            expect(screen.getByText(/occurred 2 times/)).toBeInTheDocument();
        });
    });

    // ─── Custom Fallback ─────────────────────────────────────────────────

    describe('custom fallback', () => {
        it('renders static fallback element', () => {
            render(
                <ErrorBoundary fallback={<div data-testid="custom-fallback">Custom Error</div>}>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(screen.getByTestId('custom-fallback')).toBeInTheDocument();
            expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument();
        });

        it('renders function fallback with error and reset', () => {
            const fallbackFn = ({ error, reset }) => (
                <div>
                    <span data-testid="error-msg">{error.message}</span>
                    <button onClick={reset}>Custom Reset</button>
                </div>
            );

            render(
                <ErrorBoundary fallback={fallbackFn}>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(screen.getByTestId('error-msg')).toHaveTextContent('Test error');
            expect(screen.getByText('Custom Reset')).toBeInTheDocument();
        });
    });

    // ─── onError Callback ────────────────────────────────────────────────

    describe('onError callback', () => {
        it('calls onError with error and errorInfo', () => {
            const onError = vi.fn();
            render(
                <ErrorBoundary onError={onError}>
                    <ThrowingComponent />
                </ErrorBoundary>
            );
            expect(onError).toHaveBeenCalledTimes(1);
            expect(onError).toHaveBeenCalledWith(
                expect.objectContaining({ message: 'Test error' }),
                expect.objectContaining({ componentStack: expect.any(String) })
            );
        });
    });

    // ─── withErrorBoundary HOC ───────────────────────────────────────────

    describe('withErrorBoundary', () => {
        it('wraps component with error boundary', () => {
            const SafeComponent = withErrorBoundary(ThrowingComponent);
            render(<SafeComponent />);
            expect(screen.getByText('Something went wrong')).toBeInTheDocument();
        });

        it('passes through props to wrapped component', () => {
            const SafeComponent = withErrorBoundary(ThrowingComponent);
            render(<SafeComponent shouldThrow={false} />);
            expect(screen.getByTestId('child-content')).toBeInTheDocument();
        });

        it('sets displayName correctly', () => {
            const MyComponent = () => <div>Test</div>;
            MyComponent.displayName = 'MyComponent';
            const Wrapped = withErrorBoundary(MyComponent);
            expect(Wrapped.displayName).toBe('withErrorBoundary(MyComponent)');
        });
    });
});
