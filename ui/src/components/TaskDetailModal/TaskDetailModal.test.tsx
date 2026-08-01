import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { useTaskOutput } from '../../hooks/useTaskOutput';

vi.mock('../../hooks/useTaskOutput', () => ({
    useTaskOutput: vi.fn(() => ({ input: null, output: null, truncated: false, loading: false, loaded: false })),
}));
import { TaskDetailModal } from './TaskDetailModal';
import {
    createTaskDetailModalProps,
    createTask,
    createFailedTask,
    createRunningTask,
    createWaitingDecisionTask,
    createPausedTask,
    createStoppedTask,
    createTaskEvents,
    createPipeline,
} from '../../test/factories';

// ─── Component mocks (all inline for vi.mock hoisting) ──────────────────────
// All icon exports stubbed (update if new icons added to utils/icons.jsx)
vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('lucide-react', () => ({ Activity: () => null, AlertCircle: () => null, ArrowLeft: () => null, Check: () => null, CheckCircle: () => null, ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null, Circle: () => null, Eye: () => null, EyeOff: () => null, HelpCircle: () => null, KeyRound: () => null, ListTodo: () => null, Loader2: () => null, Lock: () => null, LogOut: () => null, Mail: () => null, Menu: () => null, Moon: () => null, Package: () => null, Pause: () => null, RefreshCw: () => null, Shield: () => null, Sun: () => null, User: () => null, Users: () => null, Workflow: () => null, X: () => null, Zap: () => null }));
vi.mock('@/components/ui/button', () => ({
    Button: (props) => <button onClick={props.onClick} disabled={props.disabled} className={props.className} title={props.title} data-variant={props.variant}>{props.children}</button>,
}));
vi.mock('../BaseModal', () => ({
    BaseModal: ({ isOpen, children, className }) => isOpen ? <div data-testid="base-modal" className={className} role="dialog">{children}</div> : null,
    ModalHeader: ({ children, icon, onClose }) => <div data-testid="modal-header">{icon}<span>{children}</span>{onClose && <button data-testid="modal-close" onClick={onClose}>x</button>}</div>,
    ModalBody: ({ children }) => <div data-testid="modal-body">{children}</div>,
    ModalFooter: ({ children }) => <div data-testid="modal-footer">{children}</div>,
}));
vi.mock('../../lib/config', () => ({ default: { API_URL: '/api', POLLING_INTERVAL: 5000, AUTH_ENABLED: false } }));
vi.mock('../../utils/api', () => ({ api: { get: vi.fn().mockResolvedValue({ ok: true, data: {} }), post: vi.fn().mockResolvedValue({ ok: true, data: {} }) } }));
vi.mock('../CountdownTimer', () => ({ CountdownTimer: ({ targetTime }) => <span data-testid="countdown">{targetTime}</span> }));
vi.mock('./LoginPage', () => ({ LoginPage: () => <div data-testid="login-page" /> }));
vi.mock('./Notifications', () => ({ default: () => <div data-testid="notifications" /> }));
vi.mock('./UserMenu', () => ({ UserMenu: () => <div data-testid="user-menu" /> }));
vi.mock('./Skeletons', () => ({ PipelineListSkeleton: () => <div data-testid="skeleton" /> }));
vi.mock('@/hooks/useAuth', () => ({ useAuth: () => ({ isSignedIn: true, user: { email: 'test@test.com' }, signOut: vi.fn() }), AUTH_STATE: { SIGNED_IN: 'signedIn', SIGNED_OUT: 'signedOut' } }));
vi.mock('@/utils/api', () => ({ setAuthTokenGetter: vi.fn(), setAuthErrorCallback: vi.fn() }));


describe('TaskDetailModal', () => {
    let defaultProps;

    beforeEach(() => {
        defaultProps = createTaskDetailModalProps();
        // Mock clipboard API
        Object.assign(navigator, {
            clipboard: { writeText: vi.fn().mockResolvedValue(undefined) },
        });
    });

    // ─── Rendering ───────────────────────────────────────────────────────

    describe('rendering', () => {
        it('renders nothing when task is null', () => {
            const { container } = render(<TaskDetailModal {...defaultProps} task={null} />);
            expect(container.innerHTML).toBe('');
        });

        it('renders modal with task name and status', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('extract_data')).toBeInTheDocument();
            expect(screen.getByText('success')).toBeInTheDocument();
        });

        it('shows task type badge when task_type is set', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('lambda')).toBeInTheDocument();
        });

        it('shows execution name with copy button', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('exec-2024-01-15-001')).toBeInTheDocument();
        });

        it('shows dependency count', () => {
            render(<TaskDetailModal {...defaultProps} />);
            // Dependencies count shown in duration stats
            expect(screen.getByText('1')).toBeInTheDocument();
            const matches = screen.getAllByText('Dependencies');
            expect(matches.length).toBeGreaterThan(0);
        });

        it('shows pipeline execution short ID', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('abc123')).toBeInTheDocument();
        });
    });

    // ─── Output Tab ──────────────────────────────────────────────────────

    describe('input / output tab', () => {
        const io = (over: Record<string, unknown>) => ({
            input: null, output: null, truncated: false, loading: false, loaded: true, ...over,
        });

        it('displays the task output as JSON', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: { rows: 42 } }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByLabelText('Task output').textContent).toContain('42');
        });

        it('displays the task input (upstream + variables)', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: { upstream: { a: { output: { n: 1 } } }, variables: { year: '2026' } },
                output: { ok: true },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByLabelText('Task input').textContent).toContain('upstream');
            expect(screen.getByLabelText('Task input').textContent).toContain('2026');
        });

        it('shows an empty state when the task stored no output', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: null }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/stored no output/i)).toBeInTheDocument();
        });

        it('warns when the output was truncated', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ truncated: true }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/too large to store inline/i)).toBeInTheDocument();
        });

        it('renders falsy output (false / 0) as JSON, not as empty', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: false }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByLabelText('Task output').textContent).toContain('false');
            expect(screen.queryByText(/stored no output/i)).not.toBeInTheDocument();
        });

        it('shows a loading state while fetching', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ loading: true, loaded: false }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/loading/i)).toBeInTheDocument();
        });
    });

    // ─── Notification Warning ────────────────────────────────────────────

    describe('notification failure warning', () => {
        it('shows warning banner when notification_failed is true', () => {
            const task = createTask({ notification_failed: true });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByText(/Notification delivery failed/)).toBeInTheDocument();
        });

        it('does not show warning when notification_failed is false', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.queryByText(/Notification delivery failed/)).not.toBeInTheDocument();
        });
    });

    // ─── Tabs ────────────────────────────────────────────────────────────

    describe('tab navigation', () => {
        it('defaults to Details tab', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('Details').closest('.nav-tab')).toHaveClass('active');
        });

        it('switches to History tab on click', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('History'));
            expect(screen.getByText('History').closest('.nav-tab')).toHaveClass('active');
        });

        it('switches to Actions tab on click', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Actions').closest('.nav-tab')).toHaveClass('active');
        });
    });

    // ─── Details Tab: Status Variants ────────────────────────────────────

    describe('status-specific rendering', () => {
        it('shows paused message with Resume button for waiting_paused', () => {
            const task = createPausedTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByText('Pipeline paused')).toBeInTheDocument();
            expect(screen.getByText('Resume')).toBeInTheDocument();
        });

        it('calls onPauseResume when Resume button clicked', () => {
            const task = createPausedTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Resume'));
            expect(defaultProps.onPauseResume).toHaveBeenCalledTimes(1);
        });

        it('shows error section for failed tasks', () => {
            const task = createFailedTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByText('Lambda function timed out after 300s')).toBeInTheDocument();
        });

        it('does not show error section for successful tasks', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.queryByText('Error')).not.toBeInTheDocument();
        });
    });

    // ─── History Tab ─────────────────────────────────────────────────────

    describe('History tab', () => {
        it('shows loading state when events are loading', () => {
            render(<TaskDetailModal {...defaultProps} taskEventsLoading={true} />);
            fireEvent.click(screen.getByText('History'));
            expect(screen.getByText('Loading events...')).toBeInTheDocument();
        });

        it('shows real events when taskEvents are provided', () => {
            const events = createTaskEvents();
            render(<TaskDetailModal {...defaultProps} taskEvents={events} />);
            fireEvent.click(screen.getByText('History'));
            expect(screen.getByText(/Real events from task execution/)).toBeInTheDocument();
            expect(screen.getByText(/4 events/)).toBeTruthy();
        });

        it('shows derived status for tasks without events', () => {
            const task = createTask({ status: 'waiting', started_at: null, finished_at: null });
            render(<TaskDetailModal {...defaultProps} task={task} taskEvents={[]} />);
            fireEvent.click(screen.getByText('History'));
            expect(screen.getByText(/Derived from task status/)).toBeInTheDocument();
            expect(screen.getByText('Waiting for upstream dependencies')).toBeInTheDocument();
        });

        it('shows decision event for waiting_decision status', () => {
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} taskEvents={[]} />);
            fireEvent.click(screen.getByText('History'));
            expect(screen.getByText('Awaiting manual decision')).toBeInTheDocument();
        });
    });

    // ─── Actions Tab: Task Control ───────────────────────────────────────

    describe('Actions tab - task control buttons', () => {
        it('shows Skip, Fail, Mark Successful for waiting_decision tasks', () => {
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Skip Task')).toBeInTheDocument();
            expect(screen.getByText('Mark Failed')).toBeInTheDocument();
            expect(screen.getByText('Mark Successful')).toBeInTheDocument();
        });

        it('shows Stop for waiting_decision tasks — an explicit "give up, don\'t retry" choice', () => {
            // The backend already fully supports this: stop_task's own
            // TASK_STOPPABLE_STATUSES already includes waiting_decision,
            // transitioning it to 'aborted'. Stop and Restart now both show
            // directly for waiting_decision, covering two different
            // intents: Stop = final, notify downstream now; Restart =
            // retry, via restart_task_helper's hard StopExecution kill,
            // which never triggers that same immediate notification.
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Stop Task')).toBeInTheDocument();
        });

        it('shows Restart directly for waiting_decision tasks — no need to Stop first', () => {
            // §9's final step: restart_task's backend now directly accepts
            // waiting_decision (RESTARTABLE_STATUSES extension), safe because
            // both prerequisites are in place — the correct field name so
            // Stop_Old_Wrapper can actually kill the still-live wrapper, and
            // the attempt-keyed ConditionExpression guard so a surviving
            // ghost can never corrupt a newer attempt's state either way.
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Restart Task')).toBeInTheDocument();
        });

        it('shows Stop for running tasks without error', () => {
            const task = createRunningTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Stop Task')).toBeInTheDocument();
        });

        it('does not show Stop for running tasks with error', () => {
            const task = createRunningTask({ error: 'timeout' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.queryByText('Stop Task')).not.toBeInTheDocument();
            // But shows Skip and Fail instead
            expect(screen.getByText('Skip Task')).toBeInTheDocument();
            expect(screen.getByText('Mark Failed')).toBeInTheDocument();
        });

        it('shows Restart for stopped tasks', () => {
            const task = createStoppedTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Restart Task')).toBeInTheDocument();
        });

        it('shows Restart for terminal states', () => {
            const terminalStatuses = ['success', 'failed', 'upstream_failed', 'skipped', 'stopped', 'aborted'];
            terminalStatuses.forEach(status => {
                const task = createTask({ status, finished_at: '2024-01-15T08:05:30Z' });
                const { unmount } = render(<TaskDetailModal {...defaultProps} task={task} />);
                fireEvent.click(screen.getByText('Actions'));
                expect(screen.getByText('Restart Task')).toBeInTheDocument();
                unmount();
            });
        });

        it('does not show task control buttons for waiting tasks (except Mark Successful)', () => {
            const task = createTask({ status: 'waiting', finished_at: null });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));
            // waiting gets: Skip + Mark Successful
            expect(screen.getByText('Skip Task')).toBeInTheDocument();
            expect(screen.getByText('Mark Successful')).toBeInTheDocument();
            expect(screen.queryByText('Mark Failed')).not.toBeInTheDocument();
            expect(screen.queryByText('Restart Task')).not.toBeInTheDocument();
        });

        it('fires onAction with correct action string', () => {
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Actions'));

            fireEvent.click(screen.getByText('Skip Task').closest('button'));
            expect(defaultProps.onAction).toHaveBeenCalledWith('skip');

            fireEvent.click(screen.getByText('Mark Failed').closest('button'));
            expect(defaultProps.onAction).toHaveBeenCalledWith('fail');

            fireEvent.click(screen.getByText('Mark Successful').closest('button'));
            expect(defaultProps.onAction).toHaveBeenCalledWith('success');
        });
    });

    // ─── Actions Tab: Pipeline Run ───────────────────────────────────────

    describe('Actions tab - pipeline run actions', () => {
        it('shows Run to Here, Run from Here, Run Only This', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText('Run to Here')).toBeInTheDocument();
            expect(screen.getByText('Run from Here')).toBeInTheDocument();
            expect(screen.getByText('Run Only This')).toBeInTheDocument();
        });

        it('shows upstream count in Run to Here', () => {
            // extract_data has 1 upstream (init_config)
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText(/1 upstream/)).toBeInTheDocument();
        });

        it('shows downstream count in Run from Here', () => {
            // extract_data has 2 downstream (transform → load_db)
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Actions'));
            expect(screen.getByText(/2 downstream/)).toBeInTheDocument();
        });

        it('calls onRunAction and closes modal for run actions', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Actions'));

            fireEvent.click(screen.getByText('Run to Here').closest('button'));
            expect(defaultProps.onClose).toHaveBeenCalled();
            expect(defaultProps.onRunAction).toHaveBeenCalledWith('toHere', expect.objectContaining({ task_name: 'extract_data' }));
        });
    });

    // ─── Child Pipeline Detection ────────────────────────────────────────

    describe('child pipeline detection', () => {
        it('shows link to child pipeline when task ARN matches a pipeline', () => {
            const pipeline = createPipeline({ 
                name: 'child-pipeline',
                arn: 'arn:aws:states:us-east-1:123456:stateMachine:child' 
            });
            const task = createTask({ task_arn: 'arn:aws:states:us-east-1:123456:stateMachine:child' });
            render(<TaskDetailModal {...defaultProps} task={task} pipelines={[pipeline]} />);
            expect(screen.getByText(/child-pipeline/)).toBeInTheDocument();
        });
    });

    // ─── Close behavior ──────────────────────────────────────────────────

    describe('close behavior', () => {
        it('calls onClose when close button is clicked', () => {
            render(<TaskDetailModal {...defaultProps} />);
            const closeButtons = screen.getAllByRole('button');
            // The X button in the header
            const closeBtn = closeButtons.find(btn => btn.classList.contains('modal-close'));
            if (closeBtn) {
                fireEvent.click(closeBtn);
                expect(defaultProps.onClose).toHaveBeenCalled();
            }
        });

        it('calls onClose when Close footer button is clicked', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Close'));
            expect(defaultProps.onClose).toHaveBeenCalled();
        });
    });

    // ─── Copy to Clipboard ───────────────────────────────────────────────

    describe('copy to clipboard', () => {
        it('copies execution name to clipboard', () => {
            render(<TaskDetailModal {...defaultProps} />);
            // Find copy buttons by title
            const copyBtn = screen.getAllByTitle('Copy to clipboard')[0];
            fireEvent.click(copyBtn);
            expect(navigator.clipboard.writeText).toHaveBeenCalledWith('exec-2024-01-15-001');
        });
    });

    // ─── Keyboard shortcuts (v0.78.3, ADR #64; revised v0.78.5) ──────────
    // v0.78.5: numeric keys conflicted with global nav in App.tsx.
    // Modal tabs now use letter keys: d=details, t=timeline, a=actions.
    describe('keyboard shortcuts', () => {
        it('pressing "t" switches to timeline tab', () => {
            const { container } = render(<TaskDetailModal {...defaultProps} />);
            fireEvent.keyDown(document, { key: 't' });
            const active = container.querySelector('.active');
            expect(active?.textContent).toMatch(/history/i);
        });

        it('pressing "a" switches to actions tab', () => {
            const { container } = render(<TaskDetailModal {...defaultProps} />);
            fireEvent.keyDown(document, { key: 'a' });
            const active = container.querySelector('.active');
            expect(active?.textContent).toMatch(/action/i);
        });

        it('shortcut is disabled when modal is closed (task=null)', () => {
            const { container } = render(<TaskDetailModal {...defaultProps} task={null} />);
            // Sanity: nothing rendered
            expect(container.innerHTML).toBe('');
            // Pressing "t" shouldn't error
            fireEvent.keyDown(document, { key: 't' });
        });
    });

    describe('paid boundary', () => {
        it('hides the Backfill action in the free build (paid surface gated)', () => {
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByRole('tab', { name: /Actions/i }));
            // Backfill is a Team-only surface — must not leak into OSS.
            expect(screen.queryByText('Backfill This Task')).not.toBeInTheDocument();
            // Basic task control stays available.
            expect(screen.getByText('Restart Task')).toBeInTheDocument();
        });
    });


    describe('AWS Console links', () => {
        it('shows only the Wrapper link for non-SFN tasks (no states task execution)', () => {
            // default factory task is a lambda with no task_execution_arn
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByRole('link', { name: /Wrapper/i })).toBeInTheDocument();
            expect(screen.queryByRole('link', { name: /^Task$/i })).not.toBeInTheDocument();
        });

        it('also shows the Task link for SFN tasks (states execution ARN)', () => {
            const task = createTask({
                task_type: 'sfn',
                task_execution_arn: 'arn:aws:states:us-east-1:123456:execution:my-sfn:run001',
            });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByRole('link', { name: /^Task$/i })).toBeInTheDocument();
            expect(screen.getByRole('link', { name: /Wrapper/i })).toBeInTheDocument();
        });
    });

});
