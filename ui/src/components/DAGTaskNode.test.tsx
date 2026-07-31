/**
 * Unit tests for `TaskNode` (v0.75.7).
 *
 * `TaskNode` is the ReactFlow node component used inside `DAGGraphFlow`.
 * The parent's tests (DAGGraphFlow.test.tsx) mock ReactFlow and render
 * nodes as plain divs, which bypasses the `nodeTypes={{ task: TaskNode }}`
 * registry — so TaskNode's body never executes there. This file fills
 * that gap by rendering TaskNode directly.
 *
 * Background: in v0.75.3 three tests for the trigger_rule badge were
 * deleted from DAGGraphFlow.test.tsx with a comment that the mock made
 * them untestable. That was the wrong call (CLAUDE.md #11 — fix the
 * underlying issue, don't drop the assertion). v0.75.7 makes TaskNode
 * a named export and reinstates the tests here as proper unit cases.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TaskNode } from './DAGGraphFlow';

// ReactFlow's Handle component renders DOM nodes that need ReactFlow's
// provider context. Mock it to a no-op div so we can render TaskNode in
// isolation without dragging in the full ReactFlow runtime.
vi.mock('reactflow', () => ({
    Handle: ({ type }: { type: string }) => <div data-testid={`handle-${type}`} />,
    Position: { Left: 'left', Right: 'right' },
}));

// Icon mocks — TaskNode pulls a few from the shared icons module.
vi.mock('../utils/icons', () => ({
    StatusIcon: ({ status }: { status: string }) => <span data-testid={`status-${status}`} />,
    StalenessIcon: () => null,
    AlertTriangle: () => <span data-testid="alert-triangle" />,
    CheckCircle2: () => null,
    XCircle: () => null,
    Loader2: () => null,
    Clock: () => null,
    Settings: () => null,
    BarChart3: () => null,
    Hourglass: () => null,
    Check: () => null,
}));

vi.mock('lucide-react', () => ({
    AlertTriangle: () => <span data-testid="alert-triangle" />,
}));

// CountdownBadge has its own timing logic; not relevant to these tests.
vi.mock('./DAGGraphFlow', async () => {
    const actual: Record<string, unknown> = await vi.importActual('./DAGGraphFlow');
    return {
        ...actual,
        // Re-export TaskNode unchanged. We intentionally don't mock
        // CountdownBadge here — it accepts undefined task gracefully.
    };
});


// ─── Helpers ────────────────────────────────────────────────────────────────

function makeTask(overrides: Record<string, unknown> = {}) {
    return {
        task_name: 'sample_task',
        status: 'success' as const,
        pipeline_name: 'acme-daily',
        execution_name: 'exec-1',
        ...overrides,
    };
}

function renderNode(props: {
    label?: string;
    status?: string;
    task?: ReturnType<typeof makeTask> | null;
    selected?: boolean;
}) {
    return render(
        <TaskNode
            data={{
                label: props.label ?? 'sample',
                status: props.status ?? 'success',
                task: props.task ?? makeTask(),
                isBlueprint: false,
                serverOffsetMs: 0,
            }}
            selected={props.selected ?? false}
        />,
    );
}


// ─── Tests ──────────────────────────────────────────────────────────────────

describe('TaskNode › trigger_rule badge', () => {
    it('renders the badge when trigger_rule is non-default ("all_done")', () => {
        // Operators reading the DAG see green mark_daily_complete next to a
        // red upstream task and ask "why didn't this fail?". The badge tells
        // them: "because this task uses all_done, that's the rule".
        renderNode({
            label: 'mark_daily_complete',
            task: makeTask({ trigger_rule: 'all_done' }),
        });
        expect(screen.getByText('all_done')).toBeInTheDocument();
    });

    it('renders the badge for "one_success"', () => {
        // Other non-default rules behave the same way — any non-default
        // rule surfaces, only 'all_success' (the default) stays silent.
        renderNode({
            task: makeTask({ trigger_rule: 'one_success' }),
        });
        expect(screen.getByText('one_success')).toBeInTheDocument();
    });

    it('does NOT render the badge for default trigger_rule ("all_success")', () => {
        // Default rule means "do the obvious thing". No need to clutter
        // the DAG visualization with a badge that says "this works the
        // way you'd expect".
        renderNode({
            task: makeTask({ trigger_rule: 'all_success' }),
        });
        expect(screen.queryByText('all_success')).not.toBeInTheDocument();
    });

    it('does NOT render the badge when trigger_rule is undefined', () => {
        // An older task record without the field at all — same UX as
        // default. No badge.
        renderNode({
            task: makeTask({ /* trigger_rule omitted */ }),
        });
        expect(screen.queryByText('all_success')).not.toBeInTheDocument();
        expect(screen.queryByText('all_done')).not.toBeInTheDocument();
    });

    it('badge has a title attribute matching the rule (hover discoverability)', () => {
        renderNode({
            task: makeTask({ trigger_rule: 'all_done' }),
        });
        const badge = screen.getByText('all_done');
        // The hover-tooltip is the documented affordance; assert it
        // exists and includes the rule name verbatim.
        expect(badge.getAttribute('title')).toContain('Trigger rule');
        expect(badge.getAttribute('title')).toContain('all_done');
    });
});


describe('TaskNode › task notification warning', () => {
    it('renders alert icon when notification_failed is true', () => {
        // The Slack-notification-failed marker is critical because it tells
        // operators a task is awaiting their decision via the UI even though
        // upstream notification didn't reach them.
        renderNode({
            task: makeTask({ notification_failed: true }),
        });
        expect(screen.getByTestId('alert-triangle')).toBeInTheDocument();
    });

    it('does NOT render alert icon when notification_failed is false', () => {
        renderNode({
            task: makeTask({ notification_failed: false }),
        });
        expect(screen.queryByTestId('alert-triangle')).not.toBeInTheDocument();
    });
});


describe('TaskNode › status rendering', () => {
    it('renders the status icon matching task status', () => {
        renderNode({ status: 'running' });
        expect(screen.getByTestId('status-running')).toBeInTheDocument();
    });

    it('renders the label text', () => {
        renderNode({ label: 'extract_listings' });
        expect(screen.getByText('extract_listings')).toBeInTheDocument();
    });
});

describe('TaskNode › blueprint mode contrast (dark mode visibility fix)', () => {
    it('does not dim the whole node to 0.5 opacity', () => {
        // Previously the whole node (border AND the text/badge inside it)
        // was faded to 0.5 opacity on top of an already-subtle border
        // color — in dark mode this made the task name and type badge
        // hard to read, undermining the point of showing them at all.
        const { container } = renderNode({ status: 'blueprint', label: 'aggregate' });
        const node = container.querySelector('.dag-flow-node');
        expect(node).not.toBeNull();
        expect((node as HTMLElement).style.opacity).toBe('');
    });

    it('uses --border-strong instead of the too-subtle --border', () => {
        const { container } = renderNode({ status: 'blueprint' });
        const node = container.querySelector('.dag-flow-node') as HTMLElement;
        expect(node.style.border).toContain('var(--border-strong)');
        expect(node.style.border).not.toContain('var(--border)');
    });

    it('keeps the dashed border style that signals "not yet executed"', () => {
        const { container } = renderNode({ status: 'blueprint' });
        const node = container.querySelector('.dag-flow-node') as HTMLElement;
        expect(node.style.border).toContain('dashed');
    });

    it('label text stays at full contrast (no opacity fade)', () => {
        renderNode({ status: 'blueprint', label: 'aggregate' });
        const label = screen.getByText('aggregate');
        expect(label).toHaveClass('dag-node-label');
        // .dag-node-label's CSS uses --text-primary (full contrast); the
        // fix here is that no inline opacity on an ancestor fades it out.
    });
});
