import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// Mock next/navigation — must come before AppNav import
const pushMock = vi.fn();
let currentPathname = '/pipelines/';
vi.mock('@/hooks/useClientRoute', () => ({
    useClientRoute: () => ({ pathname: currentPathname, push: pushMock, replace: vi.fn() }),
}));
vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, History: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, Rocket: () => null, X: () => null, Zap: () => null }));

vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, Copy: () => null, Database: () => null, Download: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, Minus: () => null, Moon: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, Trash2: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, Copy: () => null, Database: () => null, Download: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, Minus: () => null, Moon: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, Trash2: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, Zap: () => null }));

vi.mock('./UserMenu', () => ({ UserMenu: () => <div data-testid="account-menu" /> }));
import { AppNav } from './AppNav';

function setPathname(p: string) {
    currentPathname = p;
}

describe('AppNav', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        setPathname('/pipelines/');
    });

    it('does not render the brand mark in the rail (it moved to the header breadcrumb)', () => {
        render(<AppNav />);
        expect(screen.queryByText(/polyris/)).not.toBeInTheDocument();
    });

    it('renders the free nav destinations (Assets/Backfills are paid, hidden in OSS)', () => {
        render(<AppNav />);
        expect(screen.getByText('Pipelines')).toBeInTheDocument();
        expect(screen.getByText('History')).toBeInTheDocument();
        // OSS paidSurface is empty → Assets & Backfills entries are not rendered.
        expect(screen.queryByText('Assets')).not.toBeInTheDocument();
        expect(screen.queryByText('Backfills')).not.toBeInTheDocument();
    });

    it('navigates via router.push when a nav item is clicked', () => {
        setPathname('/runs/');
        render(<AppNav />);
        fireEvent.click(screen.getByText('Pipelines'));
        expect(pushMock).toHaveBeenCalledWith('/pipelines/');
    });

    it('marks the active destination from the pathname', () => {
        setPathname('/runs/');
        render(<AppNav />);
        const nav = document.querySelector('.app-rail-nav');
        const runs = Array.from(nav!.querySelectorAll('.nav-pill')).find(el => el.textContent?.includes('History'));
        expect(runs?.classList.contains('active')).toBeTruthy();
    });

    it('marks History active on the tasks route too (merged view)', () => {
        setPathname('/tasks/');
        render(<AppNav />);
        const nav = document.querySelector('.app-rail-nav');
        const history = Array.from(nav!.querySelectorAll('.nav-pill')).find(el => el.textContent?.includes('History'));
        expect(history?.classList.contains('active')).toBeTruthy();
    });

    it('handles trailing slash and no slash equally', () => {
        setPathname('/runs');
        render(<AppNav />);
        const nav = document.querySelector('.app-rail-nav');
        const runs = Array.from(nav!.querySelectorAll('.nav-pill')).find(el => el.textContent?.includes('History'));
        expect(runs?.classList.contains('active')).toBeTruthy();
    });

    it('renders the account menu at the bottom of the rail', () => {
        render(<AppNav />);
        expect(screen.getByTestId('account-menu')).toBeInTheDocument();
    });

});
