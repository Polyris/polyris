import React, { useState, useMemo, useCallback, useEffect, useRef } from 'react';
import ReactFlow, {
    Background,
    BackgroundVariant,
    Controls,
    MiniMap,
    useNodesState,
    useEdgesState,
    useNodes,
    useReactFlow,
    MarkerType,
    Handle,
    Position,
    Panel
} from 'reactflow';
import dagre from 'dagre';
import { formatCountdown, formatWaitBadge, formatDuration } from '../utils';
import { TASK_SUCCESS_STATUSES, TASK_SETTLED_STATUSES } from '@/generated/enums';
import { taskTypeBadge } from '../utils/taskTypeBadge';
import { mergeNodePositions } from '../utils/reactFlowHelpers';
import { StatusIcon, CheckCircle2, XCircle, Loader2, Clock, Settings, BarChart3, Hourglass, Check, AlertTriangle } from '../utils/icons';
import { 
    TASK_STATUS, 
    isTerminalStatus, 
    getWaitingReason,
    MS 
} from '../utils/constants';
import 'reactflow/dist/style.css';
import type { DAGGraphFlowProps, CountdownBadgeProps, DAGTaskNodeData } from '@/types';

/**
 * DAGGraphFlow - Interactive Pipeline DAG using React Flow
 * 
 * Features:
 * - Interactive zoom/pan/drag
 * - Minimap for navigation
 * - Fit to screen
 * - Custom styled nodes with status indicators
 * - Animated edges for completed tasks
 * - Right-click context menu
 * - Countdown timers for wait_before
 */

// Status colors
const STATUS_COLORS: Record<string, { bg: string; border: string }> = {
    blueprint: { bg: 'var(--bg-secondary)', border: 'var(--border-strong)' },
    success: { bg: 'var(--success-light)', border: 'var(--success)' },
    succeeded: { bg: 'var(--success-light)', border: 'var(--success)' },
    running: { bg: 'var(--running-light)', border: 'var(--running)' },
    pending: { bg: 'var(--running-light)', border: 'var(--running)' },
    failed: { bg: 'var(--error-light)', border: 'var(--error)' },
    upstream_failed: { bg: 'var(--error-light)', border: 'var(--error)' },
    waiting: { bg: 'var(--bg-tertiary)', border: 'var(--border)' },
    waiting_delay: { bg: 'var(--warning-light)', border: 'var(--warning)' },
    waiting_decision: { bg: 'var(--decision-light)', border: 'var(--decision)' },
    waiting_paused: { bg: '#fef3c7', border: '#f59e0b' },
    deps_ready: { bg: 'var(--accent-light)', border: 'var(--accent)' },
    skipped: { bg: 'var(--skipped-light)', border: 'var(--skipped)' },
    stopped: { bg: 'var(--stopped-light)', border: 'var(--stopped)' },
    aborted: { bg: 'var(--aborted-light)', border: 'var(--aborted)' },
};

// Countdown Badge - has its own local tick so only this re-renders every second
const CountdownBadge = React.memo(({ task, serverOffsetMs }: CountdownBadgeProps) => {
    const [tick, setTick] = useState(() => Date.now());
    
    const status = task?.status || '';
    const waitBefore = Number(task?.wait_before || 0);
    
    // Use centralized status check
    const isTerminal = isTerminalStatus(status) || status === TASK_STATUS.STOPPED;
    
    // Only tick when actively counting down (waiting_delay), not when pending (deps_ready)
    const isActivelyCountingDown = status === TASK_STATUS.WAITING_DELAY;
    
    // Only run interval when actually counting down
    useEffect(() => {
        if (!isActivelyCountingDown || !waitBefore || isTerminal) return;
        const interval = setInterval(() => setTick(Date.now()), MS.TICK_INTERVAL);
        return () => clearInterval(interval);
    }, [isActivelyCountingDown, waitBefore, isTerminal]);
    
    // Don't show badge for terminal statuses or no wait_before
    if (isTerminal || !waitBefore || waitBefore <= 0) return null;
    
    const nowMs = tick + (serverOffsetMs || 0);
    const badge = formatWaitBadge({
        status,
        waitBefore,
        waitDelayUntilMs: task?.wait_delay_until_ms,
        waitDelayStartedMs: task?.wait_delay_started_ms,
        nowMs,
        formatFn: formatCountdown
    });
    
    if (!badge) return null;
    
    return (
        <div style={{
            position: 'absolute',
            top: '-8px',
            right: '-4px',
            fontSize: '10px',
            padding: '2px 6px',
            background: badge.type === 'countdown' ? 'var(--warning)' 
                      : badge.type === 'pending' ? 'var(--accent)' 
                      : 'var(--success)',
            color: 'white',
            borderRadius: '4px',
            fontWeight: 600,
            display: 'flex',
            alignItems: 'center',
            gap: '2px'
        }}>
            {badge.type === 'countdown' ? (
                <Hourglass size={10} />
            ) : badge.type === 'pending' ? (
                <Clock size={10} />
            ) : (
                <Check size={10} />
            )}
            {badge.text}
        </div>
    );
});

CountdownBadge.displayName = 'CountdownBadge';

// Task Node Component
//
// Exported by name so unit tests can render TaskNode directly without going
// through the ReactFlow shell. The DAGGraphFlow.test.tsx mock for ReactFlow
// (intentionally) renders nodes as plain divs that bypass the `nodeTypes`
// registry, so any TaskNode-internal behaviour (status icons, trigger_rule
// badge, notification warning) is invisible from that test file. Tests for
// TaskNode internals live in DAGTaskNode.test.tsx and exercise this export.
export const TaskNode = ({ data, selected }: { data: DAGTaskNodeData; selected: boolean }) => {
    const status = data.status || 'waiting';
    const isBlueprint = status === 'blueprint';
    const colors = STATUS_COLORS[status] || STATUS_COLORS.waiting;
    const waitingReason = getWaitingReason(data.task);

    // Compose tooltip text. trigger_rule is shown only when it diverges from
    // the default 'all_success' — operators expect default behavior silently;
    // surfacing 'all_done' / 'one_success' / etc. on hover prevents the
    // "why is mark_daily_complete green when run_classification failed?"
    // confusion (it's because trigger_rule='all_done' on the marker task).
    const triggerRule = data.task?.trigger_rule;
    const showsTriggerRule = triggerRule && triggerRule !== 'all_success';
    const baseTitle = isBlueprint ? 'Not yet executed' : (waitingReason || `Status: ${status}`);
    const titleText = showsTriggerRule
        ? `${baseTitle}\nTrigger rule: ${triggerRule}`
        : baseTitle;

    return (
        <div 
            className={`dag-flow-node ${status}`}
            title={titleText}
            style={{
                background: colors.bg,
                border: `2px ${isBlueprint ? 'dashed' : 'solid'} ${selected ? 'var(--accent)' : colors.border}`,
                borderRadius: '6px',
                padding: '8px 12px',
                minWidth: '130px',
                boxShadow: selected 
                    ? '0 0 0 3px var(--accent-light), 0 4px 12px rgba(0,0,0,0.15)' 
                    : isBlueprint ? 'none' : '0 1px 3px rgba(0,0,0,0.1)',
                transition: 'all 0.15s ease',
                cursor: isBlueprint ? 'default' : 'pointer'
            }}
        >
            <Handle type="target" position={Position.Left} style={{ background: colors.border }} />
            <Handle type="source" position={Position.Right} style={{ background: colors.border }} />
            
            {/* Live Countdown badge - only this re-renders every second */}
            <CountdownBadge task={data.task} serverOffsetMs={data.serverOffsetMs} />
            
            <div className="dag-node-row">
                <StatusIcon status={status} size={16} />
                {data.task?.notification_failed && (
                    <span title="Slack notification failed — task awaits decision via UI"><AlertTriangle size={12} style={{ color: 'var(--warning)', flexShrink: 0 }} /></span>
                )}
                <span className="dag-node-label">
                    {data.label}
                </span>
                {(() => {
                    const typeBadge = taskTypeBadge(data.task?.task_type);
                    return typeBadge ? (
                        <span
                            className="dag-node-type-badge"
                            style={{ background: typeBadge.color }}
                            title={`AWS ${typeBadge.label}`}
                        >
                            {typeBadge.label}
                        </span>
                    ) : null;
                })()}
                {/* Subtle badge for non-default trigger rule. Operators reading the DAG
                    expect 'all_success' silently; when a task uses 'all_done', 'one_success',
                    etc. the green-when-upstream-failed behavior surprises them. The badge
                    on the node makes the rule visible without opening TaskDetailModal. */}
                {showsTriggerRule && (
                    <span
                        className="dag-node-trigger-rule"
                        title={`Trigger rule: ${triggerRule}`}
                    >
                        {triggerRule}
                    </span>
                )}
            </div>
            
            {/* Duration if completed */}
            {data.duration && (
                <div className="dag-node-duration">
                    {data.duration}
                </div>
            )}
        </div>
    );
};

// Node types registry
const nodeTypes = {
    task: TaskNode
};

// Main Component
/**
 * ViewportFitController — the single place that decides when to reframe the graph.
 * Both triggers below end up calling the same `fitView`, obtained once via
 * `useReactFlow()` here rather than threaded through an `onInit` instance ref from the
 * parent — one source of truth for "how do we reach fitView", not two.
 *
 * Rendered as a *child* of `<ReactFlow>` (not called from the component that returns
 * `<ReactFlow>` as JSX): `useNodes` and `useReactFlow` read React Flow's internal
 * store, which exists only for that subtree.
 *
 * Structural refit — `signal` identifies a genuinely new graph (different pipeline,
 * date, or execution); its keys are the node ids that graph is expected to have.
 * Gated on those *specific* ids being present in the live store with a measured size —
 * not on React Flow's generic `useNodesInitialized()` boolean. That boolean answers
 * "is whatever the store currently holds fully measured", which can already be `true`
 * for the *previous* graph's nodes one render before this component's own `nodes` prop
 * actually reaches the store (switching pipelines recomputes this component's layout
 * synchronously, but the `nodes` state that reaches `<ReactFlow>` updates one render
 * later via its own effect) — trusting it fits against nodes that are about to be
 * replaced, and marks the signal "already fitted" before the real ones ever arrive.
 * Checking the live store for *this signal's own* ids removes that race outright: it
 * cannot say ready before those exact ids exist and are sized, regardless of render
 * timing or effect ordering.
 *
 * React Flow keeps a node's measured size across an update that reuses its id, so a
 * tasks-only refresh (same ids, new statuses) never un-measures anything and so never
 * re-fits — panning or zooming while a pipeline is running survives its own polls.
 *
 * No animation here: a structural change means a genuinely different graph, and
 * panning the camera across it implies a spatial relationship between "before" and
 * "after" that isn't there. The instant snap is the honest one.
 *
 * Resize refit — the container (not the graph) changed shape: a sidebar toggled, the
 * window resized. Framing rather than the identity of the graph, so it isn't gated on
 * measurement or deduped against `signal`; it always refits, debounced onto the next
 * frame the way a resize handler should be. Animated, since it's the same graph
 * settling into new bounds, not a jump to an unrelated one.
 */
function ViewportFitController({ signal, containerRef }: {
    signal: { positions: Record<string, { x: number; y: number }> };
    containerRef: React.RefObject<HTMLDivElement | null>;
}) {
    const liveNodes = useNodes();
    const { fitView } = useReactFlow();
    const lastFittedSignal = useRef<unknown>(undefined);
    const expectedIds = useMemo(() => Object.keys(signal.positions), [signal]);

    useEffect(() => {
        if (lastFittedSignal.current === signal) return;
        const ready = expectedIds.length > 0 && expectedIds.every(id => {
            const live = liveNodes.find(n => n.id === id);
            return !!live && live.width != null && live.height != null;
        });
        if (!ready) return;
        lastFittedSignal.current = signal;
        fitView({ padding: 0.2 });
    }, [liveNodes, signal, expectedIds, fitView]);

    useEffect(() => {
        if (!containerRef.current) return;
        const resizeObserver = new ResizeObserver(() => {
            requestAnimationFrame(() => fitView({ padding: 0.2, duration: 200 }));
        });
        resizeObserver.observe(containerRef.current);
        return () => resizeObserver.disconnect();
    }, [containerRef, fitView]);

    return null;
}

export function DAGGraphFlow({  
    dag, 
    tasks, 
    selectedTask: _selectedTask, 
    onSelectTask,
    serverOffsetMs,
    isBlueprint = false
 }: DAGGraphFlowProps) {
    // Step 1: Calculate layout positions ONLY when DAG structure changes (heavy operation)
    const layoutPositions = useMemo(() => {
        if (!dag?.nodes?.length) return { positions: {}, edges: [] };
        
        const g = new dagre.graphlib.Graph();
        g.setGraph({ rankdir: 'LR', nodesep: 50, ranksep: 80 });
        g.setDefaultEdgeLabel(() => ({}));

        dag.nodes.forEach((node) => {
            g.setNode(node.id, { width: 150, height: 50 });
        });

        (dag.edges || []).forEach((edge) => {
            g.setEdge(edge.from, edge.to);
        });

        dagre.layout(g);

        // Store positions by node id
        const positions: Record<string, { x: number; y: number }> = {};
        dag.nodes.forEach((node) => {
            const nodeWithPosition = g.node(node.id);
            positions[node.id] = {
                x: nodeWithPosition.x - 75,
                y: nodeWithPosition.y - 25
            };
        });

        // Create base edges (without status-dependent styling)
        const edges = (dag.edges || []).map(edge => ({
            id: `${edge.from}-${edge.to}`,
            source: edge.from,
            target: edge.to
        }));

        return { positions, edges };
    }, [dag]); // Only depends on dag structure!

    // Step 2: Build nodes with data (runs when tasks change, not on tick)
    const initialElements = useMemo(() => {
        if (!dag?.nodes?.length) return { nodes: [], edges: [] };
        
        const nodes = dag.nodes.map(node => {
            const task = tasks?.find(t => t.task_name === node.id);
            const status = isBlueprint ? 'blueprint' : (task?.status || 'waiting');
            
            // Calculate duration - use running_at (actual task start) not started_at (wrapper start)
            let duration = '';
            if (task?.running_at && task?.finished_at) {
                const ms = new Date(task.finished_at).getTime() - new Date(task.running_at).getTime();
                duration = formatDuration(ms);
            }
            
            // Merge wait_before/task_type from dag if not in task — task_type
            // always comes from the DAG structure (node.task_type), never
            // from the execution record, so it's needed even when a real
            // task IS found, not just the blueprint fallback.
            const taskWithWaitBefore = task ? {
                ...task,
                wait_before: task.wait_before || node.wait_before,
                task_type: task.task_type || node.task_type
            } : { wait_before: node.wait_before, task_type: node.task_type };
            
            return {
                id: node.id,
                type: 'task',
                position: layoutPositions.positions[node.id] || { x: 0, y: 0 },
                data: { 
                    label: node.id,
                    status,
                    duration,
                    task: taskWithWaitBefore,
                    serverOffsetMs
                }
            };
        });
        
        // Update edges with status-dependent styling
        const edges = layoutPositions.edges.map(edge => {
            if (isBlueprint) {
                return {
                    ...edge,
                    animated: false,
                    style: { 
                        stroke: 'var(--text-muted)',
                        strokeWidth: 1,
                        strokeDasharray: '5 5'
                    },
                    markerEnd: {
                        type: MarkerType.ArrowClosed,
                        color: 'var(--text-muted)'
                    }
                };
            }
            const sourceTask = tasks?.find(t => t.task_name === edge.source);
            const sourceStatus = sourceTask?.status || 'waiting';
            const isActive = sourceStatus === 'success';
            
            return {
                ...edge,
                animated: isActive,
                style: { 
                    stroke: isActive ? 'var(--success)' : 'var(--border)',
                    strokeWidth: isActive ? 2 : 1
                },
                markerEnd: {
                    type: MarkerType.ArrowClosed,
                    color: isActive ? 'var(--success)' : 'var(--border)'
                }
            };
        });
        
        return { nodes, edges };
    }, [dag, tasks, serverOffsetMs, layoutPositions, isBlueprint]);
    
    const [nodes, setNodes, onNodesChange] = useNodesState(initialElements.nodes);
    const [edges, setEdges, onEdgesChange] = useEdgesState(initialElements.edges);

    // Tracks which `dag` the currently-displayed nodes belong to, so a same-graph
    // refresh (a status poll) can preserve dragged positions while a genuine graph
    // switch cannot. This can't be answered by node id alone: `node.id` is just the
    // task name (ADR: console_api/routes/pipelines_info.py `'id': task_name`), so two
    // different pipelines can share an id (both have an "extract" task) — merging by
    // id across a `dag` change would silently carry pipeline A's dragged position onto
    // pipeline B's unrelated node of the same name.
    //
    // Starts at a sentinel, not `dag` itself: seeding it with `dag` would make the very
    // first effect run see `sameDag === true` and attempt a merge against the initial
    // state — which holds the exact same node objects that seeded it, so the merge is a
    // no-op in *value*, but `mergeNodePositions` always returns a new array, so React's
    // `setState` would not bail out the way `setNodes(initialElements.nodes)` (the
    // unchanged reference) does. That extra, avoidable `setNodes` call forces React Flow
    // to rebuild its internal node list (`ki()`), which does not carry a node's measured
    // `width`/`height` forward — only `handleBounds`, and only when `type` is unchanged —
    // so the freshly-mounted nodes would briefly "unmeasure" and re-observe for no
    // reason. The sentinel makes the first run take the `initialElements.nodes` branch
    // directly, which the mount already seeded state to — a true no-op, same as before
    // this fix existed.
    const NOT_YET_RENDERED = Symbol('not-yet-rendered');
    const lastDagRef = useRef<typeof dag | typeof NOT_YET_RENDERED>(NOT_YET_RENDERED);

    // Update nodes when dag/tasks change
    useEffect(() => {
        const sameDag = lastDagRef.current === dag;
        lastDagRef.current = dag;

        setNodes(prevNodes =>
            sameDag ? mergeNodePositions(initialElements.nodes, prevNodes) : initialElements.nodes
        );
        setEdges(initialElements.edges);
    }, [initialElements, dag, setNodes, setEdges]);
    
    // Handle node click
    const onNodeClick = useCallback((_event: React.MouseEvent, node: { id: string }) => {
        // In blueprint mode there is no real task — every node falls back to
        // a fake {status:'waiting', pipeline_name:'', execution_name:''}
        // object, which would open TaskDetailModal showing a misleading
        // status and fields that fail any real API call. Nothing meaningful
        // to show, so don't open it.
        if (isBlueprint) return;
        const task = tasks?.find(t => t.task_name === node.id) || { task_name: node.id, status: 'waiting' as const, pipeline_name: '', execution_name: '' };
        onSelectTask(task);
    }, [tasks, onSelectTask, isBlueprint]);
    
    // Handle right-click - open task modal (same as left-click now)
    const onNodeContextMenu = useCallback((_event: React.MouseEvent, node: { id: string }) => {
        _event.preventDefault();
        if (isBlueprint) return;
        if (onSelectTask) {
            const task = tasks?.find(t => t.task_name === node.id) || { task_name: node.id, status: 'waiting' as const, pipeline_name: '', execution_name: '' };
            onSelectTask(task);
        }
    }, [tasks, onSelectTask, isBlueprint]);
    
    // Minimap color
    const minimapNodeColor = useCallback((node: { data?: { status?: string } }) => {
        const status = node.data?.status || 'waiting';
        const colorMap: Record<string, string> = {
            success: '#22c55e',
            succeeded: '#22c55e',
            running: '#3b82f6',
            failed: '#ef4444',
            waiting: '#94a3b8',
            waiting_delay: '#eab308',
            waiting_paused: '#f59e0b',
            waiting_decision: '#f97316',
            deps_ready: '#6366f1',
            skipped: '#64748b',
            stopped: '#d97706',
            aborted: '#f97316'
        };
        return colorMap[status] || '#94a3b8';
    }, []);
    
    // Stats
    const stats = useMemo(() => {
        const total = dag?.nodes?.length || 0;
        const success = tasks?.filter(t => TASK_SUCCESS_STATUSES.includes(t.status)).length || 0;
        const running = tasks?.filter(t => t.status === 'running' || t.status === 'deps_ready' || t.status === 'waiting_delay').length || 0;
        const failed = tasks?.filter(t => TASK_SETTLED_STATUSES.includes(t.status) && !TASK_SUCCESS_STATUSES.includes(t.status)).length || 0;
        return { total, success, running, failed };
    }, [dag, tasks]);
    
    const containerRef = useRef<HTMLDivElement>(null);
    if (!dag?.nodes?.length) {
        return (
            <div className="dag-container dag-empty-state">
                <BarChart3 size={48} className="text-gray-300 mb-4" />
                <div className="dag-empty-msg">No tasks found for this date</div>
            </div>
        );
    }

    return (
        <div ref={containerRef} className="dag-container h-full w-full min-h-[300px]" role="figure" aria-label="Pipeline task dependency graph">
            <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeClick={onNodeClick}
                    onNodeContextMenu={onNodeContextMenu}
                    nodeTypes={nodeTypes}
                    minZoom={0.2}
                    maxZoom={2}
                    nodesConnectable={false}
                    elementsSelectable={true}
                    selectNodesOnDrag={false}
                >
                <ViewportFitController signal={layoutPositions} containerRef={containerRef} />
                <Background 
                    color="var(--border)" 
                    gap={20} 
                    size={1}
                    variant={BackgroundVariant.Dots}
                />
                <Controls 
                    position="top-left"
                    fitViewOptions={{ padding: 0.2 }}
                    style={{ 
                        background: 'var(--bg-secondary)',
                        border: '1px solid var(--border)',
                        borderRadius: '8px'
                    }}
                />
                <MiniMap 
                    nodeColor={minimapNodeColor}
                    maskColor="rgba(0, 0, 0, 0.1)"
                    style={{
                        background: 'var(--bg-secondary)',
                        border: '1px solid var(--border)',
                        borderRadius: '8px'
                    }}
                    pannable
                    zoomable
                />

                {/* Stats Panel */}
                <Panel position="top-right" className="dag-panel">
                    <div className="dag-stats-panel">
                        {isBlueprint ? (
                            <div className="dag-blueprint-label">
                                <Settings size={14} />
                                <span>Blueprint · {stats.total} tasks</span>
                            </div>
                        ) : (<>
                        <div className="dag-stat-item">
                            <CheckCircle2 size={14} className="text-green-500" />
                            <span className="dag-stat-value">{stats.success}</span>
                        </div>
                        <div className="dag-stat-item">
                            <Loader2 size={14} className="text-blue-500 animate-spin" />
                            <span className="dag-stat-value">{stats.running}</span>
                        </div>
                        <div className="dag-stat-item">
                            <XCircle size={14} className="text-red-500" />
                            <span className="dag-stat-value">{stats.failed}</span>
                        </div>
                        <div className="dag-stat-total">
                            / {stats.total}
                        </div>
                        </>)}
                    </div>
                </Panel>

                {/* Legend - hide in blueprint mode */}
                {!isBlueprint && <Panel position="bottom-left" className="dag-panel">
                    <div className="dag-legend-panel">
                        <div className="dag-legend-item" title="Task - Processing unit">
                            <Settings size={12} />
                            <span>Task</span>
                        </div>
                        <div className="dag-legend-divider" />
                        <div className="dag-legend-item" title="Task completed successfully">
                            <div className="dag-legend-swatch dag-legend-swatch--success" />
                            <span>Success</span>
                        </div>
                        <div className="dag-legend-item" title="Task is currently running">
                            <div className="dag-legend-swatch dag-legend-swatch--running" />
                            <span>Running</span>
                        </div>
                        <div className="dag-legend-item" title="Task failed with error">
                            <div className="dag-legend-swatch dag-legend-swatch--failed" />
                            <span>Failed</span>
                        </div>
                        <div className="dag-legend-item" title="Task waiting for dependencies">
                            <div className="dag-legend-swatch dag-legend-swatch--waiting" />
                            <span>Waiting</span>
                        </div>
                    </div>
                </Panel>}
            </ReactFlow>
        </div>
    );
}

export default DAGGraphFlow;
