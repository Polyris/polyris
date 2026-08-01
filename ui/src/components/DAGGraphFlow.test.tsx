import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import DAGGraphFlow from './DAGGraphFlow';
import { createDAG, createTask } from '../test/factories';

// ─── Component mocks ────────────────────────────────────────────────────────
// ResizeObserver is not available in jsdom. Captures its callback so a test can fire a
// resize deterministically instead of faking a real layout event.
let resizeCallback: (() => void) | null = null;
class MockResizeObserver {
    constructor(cb: () => void) { resizeCallback = cb; }
    observe = vi.fn();
    unobserve = vi.fn();
    disconnect = vi.fn();
}
global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

vi.mock('@/utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));
vi.mock('../utils/icons', () => ({ ActionIcons: new Proxy({}, { get: () => () => null }), Activity: () => null, AlertCircle: () => null, AlertTriangle: () => null, ArrowDown: () => null, ArrowLeft: () => null, ArrowRight: () => null, ArrowUp: () => null, Ban: () => null, BarChart3: () => null, Bell: () => null, BellRing: () => null, BookOpen: () => null, Calendar: () => null, Check: () => null, CheckCircle2: () => null, ChevronDown: () => null, ChevronLeft: () => null, ChevronRight: () => null, Circle: () => null, CircleDot: () => null, CircleHelp: () => null, ClipboardList: () => null, Clock: () => null, ContextIcons: new Proxy({}, { get: () => () => null }), Copy: () => null, Database: () => null, Download: () => null, ElementIcons: () => null, ExpandIcon: () => null, ExternalLink: () => null, Eye: () => null, FileText: () => null, Filter: () => null, Gauge: () => null, GitBranch: () => null, GitMerge: () => null, Globe: () => null, HelpCircle: () => null, History: () => null, Hourglass: () => null, Inbox: () => null, Info: () => null, Keyboard: () => null, Lightbulb: () => null, Link2: () => null, ListTodo: () => null, Loader2: () => null, LoadingIcon: () => null, MarkIcons: () => null, Minus: () => null, Moon: () => null, NavIcon: () => null, NavIcons: () => null, Network: () => null, Package: () => null, Palette: () => null, Pause: () => null, Play: () => null, PlayCircle: () => null, Plug: () => null, Plus: () => null, RefreshCw: () => null, RefreshIcon: () => null, Rewind: () => null, Rocket: () => null, RotateCcw: () => null, STALENESS_ICONS_COMPONENTS: () => null, STATUS_ICONS_COMPONENTS: () => null, Search: () => null, Settings: () => null, Siren: () => null, SkipForward: () => null, Square: () => null, StalenessIcon: () => null, StatusIcon: () => null, StopCircle: () => null, Sun: () => null, Target: () => null, Terminal: () => null, Timer: () => null, ToastIcons: () => null, Trash2: () => null, UIIcons: () => null, User: () => null, Workflow: () => null, Wrench: () => null, X: () => null, XCircle: () => null, XIcon: () => null, Zap: () => null }));

// Mock ReactFlow — renders nodes as divs for testability

/** `rf.fitView` is what `useReactFlow().fitView` (used by the real `ViewportFitController`)
 *  resolves to. `rf.measuredIds` is the test-controlled set of node ids that "have been
 *  measured" — mirrors React Flow's real lifecycle (a node exists in the DOM before its
 *  ResizeObserver ever fires) instead of a single generic boolean, so a test can put a
 *  *specific* id in an unmeasured state while others are measured — the exact shape of
 *  the race this controller exists to close. `rf.currentNodeIds` tracks whatever ids the
 *  mocked <ReactFlow> most recently received, so `useNodes()` reflects reality rather
 *  than a value the test set once and forgot to update.
 *  Hoisted so the mock factory (vitest hoists mocks above imports) can reach it. */
const rf = vi.hoisted(() => ({
    fitView: vi.fn(),
    measuredIds: new Set<string>(),
    currentNodeIds: [] as string[],
    currentEdges: [] as { id: string; style?: { stroke?: string; opacity?: number } }[],
}));

vi.mock('reactflow', () => {
    const Background = () => <div data-testid="rf-background" />;
    const Controls = () => <div data-testid="rf-controls" />;
    const MiniMap = () => <div data-testid="rf-minimap" />;
    const Panel = ({ children, position }: { children: React.ReactNode; position: string; className?: string }) => (
        <div data-testid={`rf-panel-${position}`}>{children}</div>
    );

    const ReactFlow = ({ nodes, edges, onNodeClick, onNodeContextMenu, onInit, children }: {
        nodes: { id: string; data: { label: string; status: string } }[];
        edges: { id: string; style?: { stroke?: string; opacity?: number } }[];
        onNodeClick?: (e: unknown, node: { id: string }) => void;
        onNodeContextMenu?: (e: unknown, node: { id: string }) => void;
        onInit?: (instance: { fitView: (o?: unknown) => void }) => void;
        children?: React.ReactNode;
    }) => {
        rf.currentNodeIds = (nodes ?? []).map(n => n.id);
        rf.currentEdges = edges ?? [];
        React.useEffect(() => { onInit?.(rf); }, [onInit]);
        return (
            <div data-testid="react-flow">
                {nodes?.map(n => (
                    <div
                        key={n.id}
                        data-testid={`rf-node-${n.id}`}
                        data-status={n.data?.status}
                        onClick={() => onNodeClick?.({}, { id: n.id })}
                        onContextMenu={(e) => { e.preventDefault(); onNodeContextMenu?.({ preventDefault: () => {} }, { id: n.id }); }}
                    >
                        {n.data?.label}
                    </div>
                ))}
                {edges?.map(e => <div key={e.id} data-testid={`rf-edge-${e.id}`} />)}
                {children}
            </div>
        );
    };
    ReactFlow.displayName = 'ReactFlow';

    return {
        __esModule: true,
        default: ReactFlow,
        Background,
        BackgroundVariant: { Dots: 'dots' },
        Controls,
        MiniMap,
        Panel,
        MarkerType: { ArrowClosed: 'arrowclosed' },
        Handle: ({ type, position }: { type: string; position: string }) => <div data-testid={`handle-${type}-${position}`} />,
        Position: { Left: 'left', Right: 'right', Top: 'top', Bottom: 'bottom' },
        // Real useState, not a stub that always returns the initial value: the
        // production code relies on the functional-update form (`setNodes(prev => ...)`)
        // to preserve dragged positions, and only real state supports that faithfully.
        useNodesState: (init: unknown[]) => {
            const [nodes, setNodes] = React.useState(init);
            return [nodes, setNodes, vi.fn()] as const;
        },
        useEdgesState: (init: unknown[]) => {
            const [edges, setEdges] = React.useState(init);
            return [edges, setEdges, vi.fn()] as const;
        },
        useNodes: () => rf.currentNodeIds.map(id =>
            rf.measuredIds.has(id) ? { id, width: 100, height: 40 } : { id, width: undefined, height: undefined }
        ),
        useReactFlow: vi.fn(() => ({ fitView: rf.fitView })),
    };
});

// Mock dagre for layout
vi.mock('dagre', () => {
    const mockGraph = {
        setGraph: vi.fn(),
        setDefaultEdgeLabel: vi.fn(),
        setNode: vi.fn(),
        setEdge: vi.fn(),
        node: vi.fn((_id: string) => ({ x: 100, y: 50 })),
    };
    return {
        default: {
            graphlib: { Graph: vi.fn(function () { return mockGraph; }) },
            layout: vi.fn(),
        },
    };
});

vi.mock('../utils', () => ({
    formatCountdown: (s: number) => `${s}s`,
    formatWaitBadge: () => null,
    formatDuration: (ms: number) => `${Math.round(ms / 1000)}s`,
}));
vi.mock('../utils/constants', () => ({
    TASK_STATUS: { WAITING_DELAY: 'waiting_delay', DEPS_READY: 'deps_ready', STOPPED: 'stopped' },
    isTerminalStatus: (s: string) => ['success', 'succeeded', 'failed', 'skipped'].includes(s),
    getWaitingReason: () => null,
    MS: { TICK_INTERVAL: 1000 },
}));
vi.mock('reactflow/dist/style.css', () => ({}));

// ─── Tests ──────────────────────────────────────────────────────────────────

describe('DAGGraphFlow', () => {
    const defaultProps = {
        dag: createDAG(),
        tasks: [
            createTask({ task_name: 'init_config', status: 'success' }),
            createTask({ task_name: 'extract_data', status: 'success' }),
            createTask({ task_name: 'transform', status: 'running' }),
            createTask({ task_name: 'load_db', status: 'waiting' }),
        ],
        selectedTask: null,
        onSelectTask: vi.fn(),
        serverOffsetMs: 0,
    };

    beforeEach(() => {
        vi.clearAllMocks();
    });

    // ─── Rendering ──────────────────────────────────────────────────────

    describe('rendering', () => {
        it('renders empty state when dag has no nodes', () => {
            render(<DAGGraphFlow {...defaultProps} dag={{ nodes: [], edges: [] }} />);
            expect(screen.getByText('No tasks found for this date')).toBeInTheDocument();
        });

        it('renders empty state when dag is null', () => {
            render(<DAGGraphFlow {...defaultProps} dag={null} />);
            expect(screen.getByText('No tasks found for this date')).toBeInTheDocument();
        });

        it('renders ReactFlow with correct number of nodes', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            expect(screen.getByTestId('react-flow')).toBeInTheDocument();
            expect(screen.getByTestId('rf-node-init_config')).toBeInTheDocument();
            expect(screen.getByTestId('rf-node-extract_data')).toBeInTheDocument();
            expect(screen.getByTestId('rf-node-transform')).toBeInTheDocument();
            expect(screen.getByTestId('rf-node-load_db')).toBeInTheDocument();
        });

        it('renders edges between nodes', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            expect(screen.getByTestId('rf-edge-init_config-extract_data')).toBeInTheDocument();
            expect(screen.getByTestId('rf-edge-extract_data-transform')).toBeInTheDocument();
            expect(screen.getByTestId('rf-edge-transform-load_db')).toBeInTheDocument();
        });

        it('renders Background, Controls, and MiniMap', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            expect(screen.getByTestId('rf-background')).toBeInTheDocument();
            expect(screen.getByTestId('rf-controls')).toBeInTheDocument();
            expect(screen.getByTestId('rf-minimap')).toBeInTheDocument();
        });

        it('renders stats panel with task counts', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            const statsPanel = screen.getByTestId('rf-panel-top-right');
            expect(statsPanel).toBeInTheDocument();
        });

        it('renders legend panel', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            expect(screen.getByText('Success')).toBeInTheDocument();
            expect(screen.getByText('Running')).toBeInTheDocument();
            expect(screen.getByText('Failed')).toBeInTheDocument();
            expect(screen.getByText('Waiting')).toBeInTheDocument();
        });
    });

    // ─── Blueprint Mode ─────────────────────────────────────────────────

    describe('blueprint mode', () => {
        it('shows blueprint label when isBlueprint=true', () => {
            render(<DAGGraphFlow {...defaultProps} isBlueprint={true} tasks={[]} />);
            expect(screen.getByText(/Blueprint/)).toBeInTheDocument();
        });

        it('hides legend in blueprint mode', () => {
            render(<DAGGraphFlow {...defaultProps} isBlueprint={true} tasks={[]} />);
            expect(screen.queryByText('Success')).not.toBeInTheDocument();
        });

        it('does not open the task modal on click — no real task exists to show', () => {
            // Every node falls back to a fake {status:'waiting', pipeline_name:'',
            // execution_name:''} object when no matching task is found, which is
            // always the case here (tasks=[]) — opening the modal on that would
            // show a misleading status and fields that fail any real API call.
            render(<DAGGraphFlow {...defaultProps} isBlueprint={true} tasks={[]} />);
            fireEvent.click(screen.getByTestId('rf-node-transform'));
            expect(defaultProps.onSelectTask).not.toHaveBeenCalled();
        });

        it('does not open the task modal on right-click either', () => {
            render(<DAGGraphFlow {...defaultProps} isBlueprint={true} tasks={[]} />);
            fireEvent.contextMenu(screen.getByTestId('rf-node-load_db'));
            expect(defaultProps.onSelectTask).not.toHaveBeenCalled();
        });

        it('uses --text-muted for edges instead of the too-subtle --border, with no extra opacity', () => {
            // --border is already a low-contrast token; a 0.5 opacity on
            // top of it made blueprint edges nearly invisible in dark mode
            // (#334155 at 50% against a dark page background). --text-muted
            // (#64748b in dark mode) is meaningfully more visible on its own.
            render(<DAGGraphFlow {...defaultProps} isBlueprint={true} tasks={[]} />);
            expect(rf.currentEdges.length).toBeGreaterThan(0);
            for (const edge of rf.currentEdges) {
                expect(edge.style?.stroke).toBe('var(--text-muted)');
                expect(edge.style?.opacity).toBeUndefined();
            }
        });
    });

    // ─── Interactions ───────────────────────────────────────────────────

    describe('interactions', () => {
        it('calls onSelectTask when node is clicked', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            fireEvent.click(screen.getByTestId('rf-node-transform'));
            expect(defaultProps.onSelectTask).toHaveBeenCalledTimes(1);
            expect(defaultProps.onSelectTask).toHaveBeenCalledWith(
                expect.objectContaining({ task_name: 'transform' })
            );
        });

        it('calls onSelectTask on right-click (context menu)', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            fireEvent.contextMenu(screen.getByTestId('rf-node-load_db'));
            expect(defaultProps.onSelectTask).toHaveBeenCalledWith(
                expect.objectContaining({ task_name: 'load_db' })
            );
        });

        it('creates placeholder task for unknown nodes', () => {
            const dag = createDAG({ nodes: [...createDAG().nodes, { id: 'unknown_task', outlets: [], inlets: [], skip_on_backfill: false }] });
            render(<DAGGraphFlow {...defaultProps} dag={dag} />);
            fireEvent.click(screen.getByTestId('rf-node-unknown_task'));
            expect(defaultProps.onSelectTask).toHaveBeenCalledWith(
                expect.objectContaining({ task_name: 'unknown_task', status: 'waiting' })
            );
        });
    });

    // ─── Accessibility ──────────────────────────────────────────────────

    describe('accessibility', () => {
        it('has role="figure" on container', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            const container = screen.getByRole('figure');
            expect(container).toBeInTheDocument();
        });

        it('has aria-label on container', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            const container = screen.getByRole('figure');
            expect(container).toHaveAttribute('aria-label', 'Pipeline task dependency graph');
        });
    });

    // ─── Edge Styling ───────────────────────────────────────────────────

    describe('edge styling', () => {
        it('renders all edges for the dag', () => {
            render(<DAGGraphFlow {...defaultProps} />);
            const edges = screen.getAllByTestId(/^rf-edge-/);
            expect(edges.length).toBe(3);
        });
    });

    // Note (v0.75.7): TaskNode-internal behaviour (trigger_rule badge,
    // notification warning, status icon, label) is unit-tested in
    // DAGTaskNode.test.tsx — that file imports the named TaskNode export
    // directly and bypasses the ReactFlow mock entirely. This file
    // continues to exercise wiring (edges, panel layout, callbacks);
    // node internals belong next to TaskNode itself.
    // ──────────────────────────────────────────────────────────────────────
    // Switching pipelines is client-side routing (ADR #111) and the detail query
    // uses keepPreviousData, so this component never unmounts and the container
    // never resizes — the two things React Flow's own `fitView` prop hooks into.
    // Without an explicit refit the viewport stays framed on the pipeline you just
    // left, and every switch needs "fit view" clicked by hand.
    //
    // The refit is gated on the *specific* node ids the current graph expects being
    // present and measured in the live store (`useNodes()`) — not on React Flow's
    // generic `useNodesInitialized()` boolean. That boolean can already read `true` for
    // the *previous* graph's nodes at the exact moment a new `signal` arrives (this
    // component's own layout recomputes synchronously on a `dag` change, but the
    // `nodes` state that reaches `<ReactFlow>` updates one render later via its own
    // effect) — an earlier version of this fix trusted that stale `true`, fit against
    // nodes about to be replaced, and marked the signal "already handled" before the
    // real ones ever arrived. A test that only toggled a coarse boolean couldn't catch
    // that: it looked right in the test and wrong on a real screen. Checking the live
    // store for *this signal's own* ids removes the race regardless of render timing.
    // ──────────────────────────────────────────────────────────────────────
    describe('viewport', () => {
        const dagA = createDAG({ nodes: [{ id: 'only_task', outlets: [], inlets: [], skip_on_backfill: false }], edges: [] });
        const dagB = createDAG();   // a different graph — different node ids entirely

        beforeEach(() => {
            rf.fitView.mockClear();
            rf.measuredIds.clear();
            resizeCallback = null;
        });

        it('does not fit while its own nodes are still unmeasured', () => {
            // rf.measuredIds starts empty: exactly "nodes exist in the DOM, ResizeObserver
            // hasn't fired yet" — the real gap this controller waits out.
            render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            expect(rf.fitView).not.toHaveBeenCalled();
        });

        it('fits once its own nodes are measured', () => {
            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            expect(rf.fitView).not.toHaveBeenCalled();

            rf.measuredIds.add('only_task');            // the ResizeObserver just fired
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} />);

            expect(rf.fitView).toHaveBeenCalled();
        });

        it('the race this replaces: does not fit against a stale, still-measured previous graph', () => {
            // dagA is rendered and measured first — this is the "previous graph" whose
            // staleness the old, boolean-based check could not tell apart from readiness.
            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.measuredIds.add('only_task');
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            expect(rf.fitView).toHaveBeenCalledTimes(1);
            rf.fitView.mockClear();

            // Switch to dagB (different ids). Its nodes are NOT in rf.measuredIds yet —
            // only 'only_task' (dagA's, now irrelevant) is. A check keyed on "is *anything*
            // currently measured" would wrongly say ready right here; this must not fit.
            rerender(<DAGGraphFlow {...defaultProps} dag={dagB} />);
            expect(rf.fitView).not.toHaveBeenCalled();

            // Now dagB's own nodes get measured — only then should it fit.
            dagB.nodes.forEach(n => rf.measuredIds.add(n.id));
            rerender(<DAGGraphFlow {...defaultProps} dag={dagB} />);
            expect(rf.fitView).toHaveBeenCalledTimes(1);
        });

        it('re-fits when the graph changes, without remounting', () => {
            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.measuredIds.add('only_task');
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.fitView.mockClear();

            dagB.nodes.forEach(n => rf.measuredIds.add(n.id));
            rerender(<DAGGraphFlow {...defaultProps} dag={dagB} />);   // another pipeline

            expect(rf.fitView).toHaveBeenCalled();
        });

        it('leaves the viewport alone when only statuses change', () => {
            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[]} />);
            rf.measuredIds.add('only_task');
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[]} />);
            rf.fitView.mockClear();

            // A poll updates task statuses; the graph is the same. Re-fitting here would
            // yank the view out from under anyone who has panned or zoomed. Real React
            // Flow keeps a node's measured size across an update that reuses its id, so
            // 'only_task' stays in rf.measuredIds — the mock mirrors that by simply never
            // clearing it, same as this test leaves it.
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[createTask({ task_name: 'only_task', status: 'success' })]} />);

            expect(rf.fitView).not.toHaveBeenCalled();
        });

        it('fits instantly — no animation duration — on a structural change', () => {
            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.measuredIds.add('only_task');
            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.fitView.mockClear();

            dagB.nodes.forEach(n => rf.measuredIds.add(n.id));
            rerender(<DAGGraphFlow {...defaultProps} dag={dagB} />);

            expect(rf.fitView).toHaveBeenCalledWith(expect.not.objectContaining({ duration: expect.anything() }));
        });

        it('refits when the container resizes, through the same fitView the graph-change path uses', async () => {
            render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.fitView.mockClear();

            resizeCallback?.();   // the ResizeObserver firing, e.g. a sidebar toggle

            // Debounced onto the next animation frame — real for a resize handler.
            await vi.waitFor(() => expect(rf.fitView).toHaveBeenCalled());
        });

        it('resize refit is animated, unlike a structural change — same graph settling into new bounds', async () => {
            render(<DAGGraphFlow {...defaultProps} dag={dagA} />);
            rf.fitView.mockClear();

            resizeCallback?.();

            await vi.waitFor(() => expect(rf.fitView).toHaveBeenCalledWith(expect.objectContaining({ duration: 200 })));
        });
    });

    // ──────────────────────────────────────────────────────────────────────
    // DAGGraphFlow's own responsibility here is narrow: decide *when* to preserve
    // positions (same `dag` → yes, different `dag` → no) and hand off to
    // mergeNodePositions for the actual merge. That function's own correctness — every
    // rule about which position wins — is exhaustively covered in
    // reactFlowHelpers.test.ts against real inputs, no mocking needed there at all.
    // Spying on it here checks the one thing that test file can't: whether this
    // component calls it under the right condition, not whether it computes correctly
    // once called.
    // ──────────────────────────────────────────────────────────────────────
    describe('node position preservation — the identity guard', () => {
        const dagA = createDAG({ nodes: [{ id: 'extract', outlets: [], inlets: [], skip_on_backfill: false }], edges: [] });
        // A different `dag` object that happens to reuse the same task name — the exact
        // collision a plain "merge by id" would get wrong (two pipelines can each have
        // an "extract" task; node.id is the task name, not pipeline-scoped).
        const dagAWithSameTaskName = createDAG({ nodes: [{ id: 'extract', outlets: [], inlets: [], skip_on_backfill: false }], edges: [] });

        it('does not attempt a merge on the very first render — nothing preceded it to merge with', async () => {
            const helpers = await import('../utils/reactFlowHelpers');
            const spy = vi.spyOn(helpers, 'mergeNodePositions');

            render(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[]} />);

            expect(spy).not.toHaveBeenCalled();
            spy.mockRestore();
        });

        it('preserves positions on a same-dag refresh (a status poll)', async () => {
            const helpers = await import('../utils/reactFlowHelpers');
            const spy = vi.spyOn(helpers, 'mergeNodePositions');

            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[]} />);
            spy.mockClear();   // drop the first-render call (no previous state to preserve anyway)

            rerender(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[createTask({ task_name: 'extract', status: 'running' })]} />);

            expect(spy).toHaveBeenCalled();
            spy.mockRestore();
        });

        it('does not preserve positions across a switch to a different dag, even one sharing a node id', async () => {
            const helpers = await import('../utils/reactFlowHelpers');
            const spy = vi.spyOn(helpers, 'mergeNodePositions');

            const { rerender } = render(<DAGGraphFlow {...defaultProps} dag={dagA} tasks={[]} />);
            spy.mockClear();

            rerender(<DAGGraphFlow {...defaultProps} dag={dagAWithSameTaskName} tasks={[]} />);

            expect(spy).not.toHaveBeenCalled();
            spy.mockRestore();
        });
    });

});
