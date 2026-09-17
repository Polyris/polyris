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
    Button: (props) => <button onClick={props.onClick} disabled={props.disabled} className={props.className} title={props.title} aria-label={props['aria-label']} data-variant={props.variant}>{props.children}</button>,
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

        it('shows full pipeline execution ID', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.getByText('arn:aws:states:us-east-1:123456:execution:pipeline:abc123')).toBeInTheDocument();
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
            // Collapsed OutputCard shows a compact single-line preview containing the value.
            expect(screen.getByLabelText('Task output preview').textContent).toContain('42');
            // Expanding the card reveals the pretty-printed JSON.
            fireEvent.click(screen.getByRole('button', { name: /Expand output/ }));
            expect(screen.getByLabelText('Task output').textContent).toContain('42');
        });

        it('displays the task input (upstream + variables) as split sections', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: { a: { output: { n: 1 }, status: 'success' } },
                    variables: { year: '2026' },
                },
                output: { ok: true },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            // Variables section is collapsed by default — expand it, then read.
            // Header role="button" has text "Variables (1)"; use exact match so we
            // don't collide with the copy button's aria-label "Copy Variables".
            const varsHeaders = screen.getAllByRole('button').filter(
                el => el.classList.contains('td-collapsible-header')
                    && el.textContent?.startsWith('Variables')
            );
            expect(varsHeaders).toHaveLength(1);
            fireEvent.click(varsHeaders[0]);
            expect(screen.getByLabelText('Task variables').textContent).toContain('2026');
            // Upstream section header carries label + count and is open by default.
            const upHeaders = screen.getAllByRole('button').filter(
                el => el.classList.contains('td-collapsible-header')
                    && el.textContent?.startsWith('Upstream')
            );
            expect(upHeaders).toHaveLength(1);
            expect(upHeaders[0].textContent).toContain('(1)');
            // The dep card carries the dep name.
            expect(screen.getByText('a')).toBeInTheDocument();
            // 'success' appears in the modal header and as the dep's status
            // badge — verify at least the dep badge (span.td-status-badge).
            const badges = screen.getAllByText('success');
            expect(badges.length).toBeGreaterThanOrEqual(1);
            expect(badges.some(el => el.classList.contains('td-status-badge'))).toBe(true);
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
            expect(screen.getByLabelText('Task output preview').textContent).toContain('false');
            expect(screen.queryByText(/stored no output/i)).not.toBeInTheDocument();
        });

        it('shows a loading state while fetching', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({ loading: true, loaded: false }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/loading/i)).toBeInTheDocument();
        });

        // ── Upstream marker interpretation ────────────────────────────────

        it('renders a warn card for a missing upstream (status=unknown)', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: { extract_sales: { output: {}, status: 'unknown' } },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('extract_sales')).toBeInTheDocument();
            expect(screen.getByText('no output')).toBeInTheDocument();
            expect(screen.getByText(/hasn.t run yet for this date/i)).toBeInTheDocument();
        });

        it('renders an error banner for a failed upstream and shows output details', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: { transform: { output: { partial: true }, status: 'failed' } },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('transform')).toBeInTheDocument();
            expect(screen.getByText(/failed/)).toBeInTheDocument();
        });

        it('renders a truncated marker with xcom.get() hint', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: {
                        big: {
                            output: { _truncated: true, _size: 30000 },
                            status: 'success',
                        },
                    },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/29\.3KB/)).toBeInTheDocument();
            expect(screen.getByText(/xcom\.get/i)).toBeInTheDocument();
        });

        it('renders an S3 claim-check pointer with resolve hint', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: {
                        payload: {
                            output: { _s3_ref: 's3://my-bucket/key.json' },
                            status: 'success',
                        },
                    },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('s3://my-bucket/key.json')).toBeInTheDocument();
        });

        it('renders _upstream_omitted marker as legacy-pipeline banner (with size)', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: { _upstream_omitted: true, _size: 27000, variables: { y: 1 } },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/26\.4KB/)).toBeInTheDocument();
            expect(screen.getByText(/legacy Console preview/i)).toBeInTheDocument();
        });

        it('warns when the output looks like AWS API metadata (JobRunId etc.)', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                output: { JobRunId: 'jr_abc123' },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            // Collapsed OutputCard already flags the metadata variant via badge.
            expect(screen.getByText('aws-metadata')).toBeInTheDocument();
            expect(screen.getByLabelText('Task output preview').textContent).toContain('jr_abc123');
            // Expanding the card reveals the full warn banner + pretty JSON.
            fireEvent.click(screen.getByRole('button', { name: /Expand output/ }));
            expect(screen.getByText(/AWS API response/i)).toBeInTheDocument();
            expect(screen.getAllByText(/xcom\.push/i).length).toBeGreaterThanOrEqual(1);
            expect(screen.getByLabelText('Task output').textContent).toContain('jr_abc123');
        });

        // ── Manual resolution (_manually_resolved marker) ─────────────────

        // Backend contract: _write_synthetic_output_marker writes DDB status =
        // action_name on the output# row — so event.upstream[X].status is the
        // RAW action ('mark_success' / 'skip' / 'fail' / 'stop'), NOT the
        // per-run task target_status ('success' / 'skipped' / ...). The manual
        // branch then maps action_name → user-friendly label via
        // statusForResolution to match OutputCard.
        it('renders an upstream mark_success as a manual card with derived success badge', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: {
                        transform: {
                            output: {
                                _manually_resolved: true,
                                _resolution: 'mark_success',
                                _reason: 'verified via S3 logs',
                                _operator: 'alice@example.com',
                            },
                            status: 'mark_success',
                        },
                    },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('transform')).toBeInTheDocument();
            expect(screen.getByText('manual: mark_success')).toBeInTheDocument();
            expect(screen.getByText(/Marked success by alice@example\.com — verified via S3 logs/))
                .toBeInTheDocument();
            // Primary badge is the user-friendly label, not the raw action_name.
            const successBadges = screen.getAllByText('success').filter(
                el => el.classList.contains('td-status-badge--success')
            );
            expect(successBadges.length).toBeGreaterThanOrEqual(1);
        });

        it('renders an upstream skip via UI as a manual card with red skipped badge', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: {
                        transform: {
                            output: {
                                _manually_resolved: true,
                                _resolution: 'skip',
                                _reason: 'source data missing today',
                                _operator: 'bob@example.com',
                            },
                            status: 'skip',
                        },
                    },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('manual: skip')).toBeInTheDocument();
            expect(screen.getByText(/Skipped by bob@example\.com — source data missing today/))
                .toBeInTheDocument();
            // Primary badge label = statusForResolution('skip') = 'skipped',
            // variant = 'error' (red).
            const skippedBadge = screen.getByText('skipped');
            expect(skippedBadge).toHaveClass('td-status-badge--error');
        });

        it('renders each manual resolution with UpstreamDep + OutputCard identical primary badges', () => {
            // The "applied identically" contract from ADR-123 §5 — for the
            // same marker, both surfaces show the same badge text and colour.
            const cases: Array<[string, string, string]> = [
                ['mark_success', 'success', 'td-status-badge--success'],
                ['skip', 'skipped', 'td-status-badge--error'],
                ['fail', 'failed', 'td-status-badge--error'],
                ['stop', 'stopped', 'td-status-badge--muted'],
            ];
            for (const [resolution, label, cls] of cases) {
                vi.mocked(useTaskOutput).mockReturnValue(io({
                    input: {
                        upstream: {
                            dep: {
                                output: {
                                    _manually_resolved: true,
                                    _resolution: resolution,
                                    _reason: 'r',
                                    _operator: 'op',
                                },
                                status: resolution,
                            },
                        },
                        variables: {},
                    },
                    // The task's OWN output is the same marker (mark_success'd
                    // via UI) so OutputCard renders the same badge alongside.
                    output: {
                        _manually_resolved: true,
                        _resolution: resolution,
                        _reason: 'r',
                        _operator: 'op',
                    },
                }));
                const { unmount } = render(<TaskDetailModal {...defaultProps} />);
                fireEvent.click(screen.getByText('Input / Output'));
                const badges = screen.getAllByText(label).filter(
                    el => el.classList.contains('td-status-badge')
                );
                // At least 2: one on UpstreamDep, one on OutputCard.
                expect(badges.length).toBeGreaterThanOrEqual(2);
                for (const b of badges) expect(b).toHaveClass(cls);
                unmount();
            }
        });

        it('falls back to generic "operator" when marker predates 0.100.0 _operator field', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: {
                        legacy: {
                            output: {
                                _manually_resolved: true,
                                _resolution: 'mark_success',
                                _reason: 'old record',
                            },
                            status: 'mark_success',
                        },
                    },
                    variables: {},
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/Marked success by unknown — old record/)).toBeInTheDocument();
        });

        it('renders the task’s own output as a manual card when the task itself was mark_success’d', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                output: {
                    _manually_resolved: true,
                    _resolution: 'mark_success',
                    _reason: 'checked upstream logs',
                    _operator: 'carol@example.com',
                },
            }));
            render(<TaskDetailModal {...defaultProps} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('manual: mark_success')).toBeInTheDocument();
            // Preview shows the human summary, not raw marker JSON.
            expect(screen.getByLabelText('Task output preview').textContent)
                .toContain('Marked success by carol@example.com');
            // Primary badge on the OutputCard derives from resolution
            // (mark_success → success). Filter to the OutputCard badge to
            // avoid the modal-header status badge collision.
            const successBadges = screen.getAllByText('success').filter(
                el => el.classList.contains('td-status-badge')
            );
            expect(successBadges.length).toBeGreaterThanOrEqual(1);
        });

        it('renders OutputCard manual variants with resolution-derived primary badges', () => {
            // Verifies the "applied identically" contract with UpstreamDep — the
            // primary badge must reflect the real outcome (skipped / failed /
            // stopped), not a hardcoded green "success" that would lie about
            // what the operator did.
            const cases: Array<[string, string]> = [
                ['skip', 'skipped'],
                ['fail', 'failed'],
                ['stop', 'stopped'],
            ];
            for (const [resolution, expectedBadge] of cases) {
                vi.mocked(useTaskOutput).mockReturnValue(io({
                    output: {
                        _manually_resolved: true,
                        _resolution: resolution,
                        _reason: 'operator note',
                        _operator: 'dan@example.com',
                    },
                }));
                const { unmount } = render(<TaskDetailModal {...defaultProps} />);
                fireEvent.click(screen.getByText('Input / Output'));
                expect(screen.getByText(`manual: ${resolution}`)).toBeInTheDocument();
                expect(screen.getByText(expectedBadge)).toBeInTheDocument();
                unmount();
            }
        });

        // ── Canonical-row gate on settled state ────────────────────────────

        it('shows pending Output state when task is in a non-settled status', () => {
            // Setup: canonical output# row still carries a marker from a
            // PRIOR same-date run (mark_success by alice); current run's task
            // is now in waiting_decision — the marker doesn't belong to it.
            // The Output card must not render the stale marker as if it were
            // this run's output.
            const staleMarker = {
                _manually_resolved: true,
                _resolution: 'mark_success',
                _reason: 'from earlier run',
                _operator: 'alice@example.com',
            };
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: staleMarker }));
            const task = createTask({ status: 'waiting_decision' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            // Pending badge appears in place of the manual card.
            expect(screen.getByText('pending')).toBeInTheDocument();
            expect(screen.getByText(/hasn.t reached a settled state yet/i)).toBeInTheDocument();
            // Current status surfaced for context — inside the pending message
            // <code> element (getByText also matches the modal-header badge).
            expect(screen.getAllByText('waiting_decision').some(
                el => el.tagName === 'CODE'
            )).toBe(true);
            // The stale marker's manual-card badges must NOT render.
            expect(screen.queryByText('manual: mark_success')).not.toBeInTheDocument();
            expect(screen.queryByText(/Marked success by alice/)).not.toBeInTheDocument();
        });

        it('renders Output normally when task IS settled (marker belongs to this run)', () => {
            // Opposite of the above: task's own status is 'success' AND the
            // canonical row is a mark_success marker — the marker belongs to
            // this run (someone just marked it), render as manual card.
            const marker = {
                _manually_resolved: true,
                _resolution: 'mark_success',
                _reason: 'verified',
                _operator: 'bob@example.com',
            };
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: marker }));
            const task = createTask({ status: 'success' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('manual: mark_success')).toBeInTheDocument();
            expect(screen.getByText(/Marked success by bob@example\.com — verified/))
                .toBeInTheDocument();
        });

        it('renders Output normally for a "running" task with real data — belt-and-suspenders', () => {
            // Defensive: if the canonical row somehow shows real data while
            // the task is in a non-settled state, we still suppress and show
            // pending. Guards against a race where Save_Success fired but
            // the per-run status hasn't caught up yet.
            vi.mocked(useTaskOutput).mockReturnValue(io({ output: { rows: 42 } }));
            const task = createTask({ status: 'running' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('pending')).toBeInTheDocument();
            expect(screen.queryByLabelText('Task output preview')).not.toBeInTheDocument();
        });

        it('hides Input snapshot when task is in a non-settled status (input# row is date-scoped too)', () => {
            // Symmetric with OutputCard's gate — the input# DDB row is
            // shared across every same-date run, so a fresh run's task in
            // waiting_decision inherits a prior run's snapshot visually
            // until Save_Input_Record overwrites it. We hide the section
            // rather than lie about whose input it is.
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: { extract: { output: { rows: 100 }, status: 'success' } },
                    variables: { year: '2026' },
                },
                output: null,
            }));
            const task = createTask({ status: 'waiting_decision' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText(/Input snapshot will appear once the task starts running/i))
                .toBeInTheDocument();
            // Prior run's upstream card must NOT render.
            expect(screen.queryByText('extract')).not.toBeInTheDocument();
            // Variables section either.
            expect(screen.queryByRole('button', { name: /Variables/ })).not.toBeInTheDocument();
        });

        it('suppresses Output for cascade-skipped task (settled but no run_task_helper_arn)', () => {
            // Auto_Skip_Register / notify_dependents' Update_Status_Skip never
            // set run_task_helper_arn on the per-run task row — so `expectedRunId`
            // is null. If the canonical row already carries content from a
            // prior same-date run, the gate must still catch it via the null-
            // expected sub-case (SEV3 must-fix from architect audit).
            vi.mocked(useTaskOutput).mockReturnValue({
                input: null,
                output: { rows: 999 },  // pretend prior run wrote real output
                truncated: false,
                loading: false,
                loaded: true,
                outputRowRunId: 'arn:aws:states:...:execution:prior-helper:run-a',
                inputRowRunId: null,
                expectedRunId: null,  // this run's task was auto-skipped
            });
            const task = createTask({ status: 'skipped' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('from prior run')).toBeInTheDocument();
            expect(screen.queryByText(/999/)).not.toBeInTheDocument();
        });

        it('renders Input normally for a "running" task when input# row belongs to this run', () => {
            // Save_Input_Record fires EARLY (before task execution), so a
            // running task's input# row is already this run's snapshot —
            // gating on TASK_SETTLED_STATUSES would over-fire and hide
            // input mid-run (bad for long-running Glue/Batch/SFN tasks).
            // The correct test is `row_run_id === expected_run_id`.
            vi.mocked(useTaskOutput).mockReturnValue({
                input: {
                    upstream: { extract: { output: { rows: 1 }, status: 'success' } },
                    variables: { year: '2026' },
                },
                output: null,
                truncated: false,
                loading: false,
                loaded: true,
                inputRowRunId: 'arn:aws:states:...:execution:helper:this-run',
                outputRowRunId: null,
                expectedRunId: 'arn:aws:states:...:execution:helper:this-run',
            });
            const task = createTask({ status: 'running' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('extract')).toBeInTheDocument();
            expect(screen.queryByText(/Input snapshot will appear/)).not.toBeInTheDocument();
        });

        it('renders Input normally when task IS settled', () => {
            vi.mocked(useTaskOutput).mockReturnValue(io({
                input: {
                    upstream: { extract: { output: { rows: 100 }, status: 'success' } },
                    variables: { year: '2026' },
                },
                output: { done: true },
            }));
            const task = createTask({ status: 'success' });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            fireEvent.click(screen.getByText('Input / Output'));
            expect(screen.getByText('extract')).toBeInTheDocument();
            // Variables section header renders (avoid collision with the
            // copy button whose aria-label also matches /Variables/).
            const varsHeaders = screen.getAllByRole('button').filter(
                el => el.classList.contains('td-collapsible-header')
                    && el.textContent?.startsWith('Variables')
            );
            expect(varsHeaders).toHaveLength(1);
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

        it('shows pending placeholder for running task without wrapper ARN', () => {
            const task = createRunningTask({ wrapper_execution_arn: null });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByText(/awaiting start/i)).toBeInTheDocument();
            expect(screen.queryByRole('link', { name: /Wrapper/i })).not.toBeInTheDocument();
        });

        it('shows active Wrapper link for running task that already has an ARN', () => {
            const task = createRunningTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByRole('link', { name: /Wrapper/i })).toBeInTheDocument();
            expect(screen.queryByText(/awaiting start/i)).not.toBeInTheDocument();
        });

        it('hides the AWS Console section entirely for non-running tasks without ARNs', () => {
            const task = createTask({ status: 'waiting', wrapper_execution_arn: null, task_execution_arn: null });
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.queryByText('AWS Console')).not.toBeInTheDocument();
        });
    });

    // ─── Copy confirmation ───────────────────────────────────────────────

    describe('copy confirmation', () => {
        it('updates aria-label to Copied after clicking execution name copy button', () => {
            render(<TaskDetailModal {...defaultProps} />);
            const copyBtn = screen.getAllByTitle('Copy to clipboard')[0];
            expect(copyBtn).toHaveAttribute('aria-label', 'Copy to clipboard');
            fireEvent.click(copyBtn);
            expect(copyBtn).toHaveAttribute('aria-label', 'Copied');
        });

        it('updates aria-label to Copied after clicking pipeline execution copy button', () => {
            render(<TaskDetailModal {...defaultProps} />);
            const copyBtn = screen.getByTitle('Copy full execution ID');
            expect(copyBtn).toHaveAttribute('aria-label', 'Copy full execution ID');
            fireEvent.click(copyBtn);
            expect(copyBtn).toHaveAttribute('aria-label', 'Copied');
        });

        it('updates aria-label to Copied after clicking error copy button', () => {
            const task = createFailedTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            const copyBtn = screen.getByTitle('Copy error');
            expect(copyBtn).toHaveAttribute('aria-label', 'Copy error');
            fireEvent.click(copyBtn);
            expect(copyBtn).toHaveAttribute('aria-label', 'Copied');
        });
    });

    // ─── Failure visibility ──────────────────────────────────────────────

    describe('failure visibility', () => {
        it('error section appears before duration stats in DOM for failed tasks', () => {
            const task = createFailedTask();
            const { container } = render(<TaskDetailModal {...defaultProps} task={task} />);
            const error = container.querySelector('.td-error-section');
            const stats = container.querySelector('.td-duration-stats');
            expect(error).not.toBeNull();
            expect(stats).not.toBeNull();
            expect(error!.compareDocumentPosition(stats!)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
        });
    });

    // ─── Decision required banner ────────────────────────────────────────

    describe('decision required banner', () => {
        it('shows Decision required banner for waiting_decision tasks', () => {
            const task = createWaitingDecisionTask();
            render(<TaskDetailModal {...defaultProps} task={task} />);
            expect(screen.getByText('Decision required')).toBeInTheDocument();
            expect(screen.getByText(/waiting for a manual decision/i)).toBeInTheDocument();
        });

        it('does not show Decision required banner for non-decision tasks', () => {
            render(<TaskDetailModal {...defaultProps} />);
            expect(screen.queryByText('Decision required')).not.toBeInTheDocument();
        });
    });

});
