import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useAppStore } from '../stores/useAppStore';
import type { PipelineWithUI } from '../types';

// Mock next/navigation — must come before Header import
const pushMock = vi.fn();
let currentPathname = '/pipelines/';
vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: currentPathname, push: pushMock, replace: vi.fn() }),
}));

vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, History: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, Rocket: () => null, X: () => null, Zap: () => null }));
vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, Copy: () => null, Database: () => null, Download: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, Minus: () => null, Moon: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, Trash2: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, Copy: () => null, Database: () => null, Download: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, Minus: () => null, Moon: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, Trash2: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, Zap: () => null }));
vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => <button onClick={props.onClick as () => void} title={props.title as string}>{props.children as React.ReactNode}</button>,
}));
vi.mock('./Notifications', () => ({ default: () => <div data-testid="notifications" /> }));
vi.mock('./UserMenu', () => ({ UserMenu: () => <div data-testid="user-menu" /> }));
vi.mock('../lib/config', () => ({ default: { API_URL: '/api', POLLING_INTERVAL: 5000, AUTH_ENABLED: false } }));
vi.mock('../utils/api', () => ({ api: { get: vi.fn(), post: vi.fn() }, setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));
vi.mock('@/utils/api', () => ({ api: { get: vi.fn(), post: vi.fn() }, setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));
vi.mock('../utils/storage', () => ({ getFromStorage: vi.fn(), saveToStorage: vi.fn() }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ isSignedIn: true, user: { email: 'test@test.com' }, signOut: vi.fn() }), AUTH_STATE: { SIGNED_IN: 'signedIn', SIGNED_OUT: 'signedOut' } }));

import { Header } from './Header';

function setPathname(p: string) {
    currentPathname = p;
}

function resetStore(overrides: Record<string, unknown> = {}) {
    const store = useAppStore.getState();
    store.setDate((overrides.date ?? '2024-01-15') as string);
    store.setLiveMode((overrides.liveMode ?? true) as boolean);
    store.setTheme((overrides.theme ?? 'light') as string);
    store.setSidebarOpen((overrides.sidebarOpen ?? false) as boolean);
    store.setSelectedPipeline((overrides.selectedPipeline ?? null) as PipelineWithUI | null);
    store.setSelectedExecution((overrides.selectedExecution ?? null) as null);
    store.setShowHelpModal(false);
}

describe('Header', () => {
    const props = {
        apiConnected: true,
        onNotificationNavigate: vi.fn(),
    };

    beforeEach(() => {
        vi.clearAllMocks();
        setPathname('/pipelines/');
        resetStore();
    });

    describe('rendering', () => {
        it('renders Notifications', () => {
            render(<Header {...props} />);
            expect(screen.getByTestId('notifications')).toBeInTheDocument();
        });

        it('no longer renders the user menu in the header (moved to the rail)', () => {
            render(<Header {...props} />);
            expect(screen.queryByTestId('user-menu')).not.toBeInTheDocument();
        });

        it('renders the polyris home breadcrumb', () => {
            render(<Header {...props} />);
            expect(screen.getByText('polyris')).toBeInTheDocument();
        });
    });

    describe('date picker', () => {
        // The topbar date picker was removed: the pipeline page scopes the date in its
        // execution-history drawer, and All Runs / All Tasks each have their own inline
        // date picker. So the header should show no date picker in any view.
        it('shows no date picker in the header (runs)', () => {
            setPathname('/runs/');
            render(<Header {...props} />);
            expect(screen.queryByText('Today')).not.toBeInTheDocument();
            expect(screen.queryByDisplayValue('2024-01-15')).not.toBeInTheDocument();
        });

        it('shows no date picker in the header (pipelines)', () => {
            render(<Header {...props} />);
            expect(screen.queryByText('Today')).not.toBeInTheDocument();
        });

        it('shows no date picker in the header (tasks)', () => {
            setPathname('/tasks/');
            render(<Header {...props} />);
            expect(screen.queryByText('Today')).not.toBeInTheDocument();
        });
    });

    describe('API status', () => {
        it('shows connected', () => {
            render(<Header {...props} apiConnected={true} />);
            expect(screen.getByTitle('API connected')).toBeInTheDocument();
        });

        it('shows disconnected', () => {
            render(<Header {...props} apiConnected={false} />);
            expect(screen.getByTitle('API disconnected')).toBeInTheDocument();
        });
    });

    describe('live mode', () => {
        it('shows Auto when enabled', () => {
            render(<Header {...props} />);
            expect(screen.getByText('Auto')).toBeInTheDocument();
        });

        it('shows Paused when disabled', () => {
            resetStore({ liveMode: false });
            render(<Header {...props} />);
            expect(screen.getByText('Paused')).toBeInTheDocument();
        });

        it('toggles store on click', () => {
            render(<Header {...props} />);
            fireEvent.click(screen.getByTitle(/auto-refresh/i));
            expect(useAppStore.getState().liveMode).toBe(false);
        });
    });

    describe('theme toggle', () => {
        it('no longer lives in the header — it moved into the account menu', () => {
            render(<Header {...props} />);
            expect(screen.queryByTitle(/Switch to/)).not.toBeInTheDocument();
        });
    });

    describe('help button', () => {
        it('no longer lives in the header — it moved into the UserMenu dropdown', () => {
            render(<Header {...props} />);
            expect(screen.queryByTitle('Help & Documentation')).not.toBeInTheDocument();
        });
    });

    describe('mobile hamburger', () => {
        it('renders in pipelines view', () => {
            render(<Header {...props} />);
            expect(screen.getByTitle('Toggle sidebar')).toBeInTheDocument();
        });

        it('toggles sidebar in store', () => {
            render(<Header {...props} />);
            fireEvent.click(screen.getByTitle('Toggle sidebar'));
            expect(useAppStore.getState().sidebarOpen).toBe(true);
        });
    });

    describe('breadcrumbs', () => {
        it('shows pipeline name when selected', () => {
            resetStore({ selectedPipeline: { name: 'acme-daily' } as PipelineWithUI });
            render(<Header {...props} />);
            expect(screen.getByText('acme-daily')).toBeInTheDocument();
        });

        it('hides pipeline name when not selected', () => {
            render(<Header {...props} />);
            expect(screen.queryByText('acme-daily')).not.toBeInTheDocument();
        });

        it('starts the trail with the section name', () => {
            resetStore({ selectedPipeline: { name: 'acme-daily' } as PipelineWithUI });
            render(<Header {...props} />);
            expect(screen.getByText('Pipelines')).toBeInTheDocument();
        });

        it('prefixes the execution crumb with "Run"', () => {
            resetStore({
                selectedPipeline: { name: 'acme-daily' } as PipelineWithUI,
                selectedExecution: { execution_short: 'abc12345' } as unknown,
            });
            render(<Header {...props} />);
            expect(screen.getByText((_c, el) => el?.textContent === 'Run abc12345')).toBeInTheDocument();
        });

        it('shows the execution crumb for the auto-selected latest run', () => {
            resetStore({
                selectedPipeline: { name: 'acme-daily' } as PipelineWithUI,
                selectedExecution: { execution_short: 'abc12345', auto_selected: true } as unknown,
            });
            render(<Header {...props} />);
            expect(screen.getByText((_c, el) => el?.textContent === 'Run abc12345')).toBeInTheDocument();
        });

        it('navigates to the section when the section crumb is clicked', () => {
            resetStore({ selectedPipeline: { name: 'acme-daily' } as PipelineWithUI });
            render(<Header {...props} />);
            fireEvent.click(screen.getByText('Pipelines'));
            expect(pushMock).toHaveBeenCalledWith('/pipelines/');
        });

        it('shows a properly-cased "Backfills" label, not the raw lowercase mainView', () => {
            // Was missing from SECTION_LABEL entirely — would have fallen
            // through to the raw pathname-derived string "backfills"
            // instead of matching every other section's Title Case label.
            setPathname('/backfills/');
            render(<Header {...props} />);
            expect(screen.getByText('Backfills')).toBeInTheDocument();
            expect(screen.queryByText('backfills')).not.toBeInTheDocument();
        });
    });

    describe('Backfills nav tab (paid surface)', () => {
        const props = { apiConnected: true, onNotificationNavigate: vi.fn() };

        it('does not render a Backfills tab in the OSS build (empty paid surface)', () => {
            render(<Header {...props} />);
            // OSS paidSurface is an empty stub (ADR #99): BackfillNavTab is undefined
            // and there is no static fallback — Backfills is a paid-only surface.
            expect(screen.queryByText('Backfills')).not.toBeInTheDocument();
        });
    });
});
