import { describe, it, expect } from 'vitest';
import { buildSidebarLines, FAILURE_STATUSES, groupKey, needsAttention, UNGROUPED } from './pipelineSidebar';
import type { SidebarLine } from './pipelineSidebar';
import type { PipelineWithUI } from '@/types';

const p = (name: string, extra: Partial<PipelineWithUI> = {}): PipelineWithUI =>
    ({ name, status: 'success', ...extra }) as PipelineWithUI;

/** One readable token per line: `§attention 2`, `▸Orders 3 open`, `orders-clean`, `  orders-clean` (indented). */
const show = (lines: SidebarLine[]) => lines.map(l => {
    if (l.kind === 'section') return `§${l.section} ${l.count}${l.open ? '' : ' folded'}`;
    if (l.kind === 'group') return `▸${l.label || '(ungrouped)'} ${l.count} ${l.open ? 'open' : 'folded'}`;
    return `${l.indented ? '  ' : ''}${l.pipeline.name}`;
});

const build = (pipelines: PipelineWithUI[], search = '', folded: string[] = [], attentionFolded = false) =>
    buildSidebarLines(pipelines, { search, folded, attentionFolded });

describe('buildSidebarLines', () => {
    it('lists pipelines A–Z with no headers when none needs attention or has a group', () => {
        expect(show(build([p('zeta'), p('alpha'), p('mid')]))).toEqual(['alpha', 'mid', 'zeta']);
    });

    it('never groups by name prefix: only DAG(group=) makes a group', () => {
        expect(show(build([p('acme-daily'), p('acme-weekly')]))).toEqual(['acme-daily', 'acme-weekly']);
    });

    it('pins failed and timed-out pipelines on top, A–Z; they stay in the full list too', () => {
        expect(FAILURE_STATUSES).toEqual(['failed', 'timed_out']);
        const lines = build([p('b-timeout', { status: 'timed_out' }), p('c-ok'), p('a-fail', { status: 'failed' })]);
        expect(show(lines)).toEqual([
            '§attention 2', 'a-fail', 'b-timeout',
            '§all 3', 'a-fail', 'b-timeout', 'c-ok',
        ]);
    });

    it('pins a run that waits for a decision, not one that is paused', () => {
        const lines = build([
            p('a-decide', { status: 'running', waiting_on: 'waiting_decision' }),
            p('b-paused', { status: 'running', waiting_on: 'waiting_paused' }),
        ]);
        expect(show(lines)).toEqual(['§attention 1', 'a-decide', '§all 2', 'a-decide', 'b-paused']);
    });

    it.each(['running', 'aborted', 'recovered', 'success', 'idle', 'unknown-status', 'constructor', undefined])('does not pin a pipeline whose last run is %s', status => {
        expect(show(build([p('one', { status })]))).toEqual(['one']);
        expect(needsAttention({ status })).toBe(false);
    });

    it('folding "Needs attention" keeps its header and count', () => {
        const lines = build([p('a-fail', { status: 'failed' }), p('b')], '', [], true);
        expect(show(lines)).toEqual(['§attention 1 folded', '§all 2', 'a-fail', 'b']);
        expect(lines[0]).toMatchObject({ foldable: true, open: false });
    });

    it('groups by group=, A–Z, with Ungrouped last; rows under a group are indented', () => {
        const lines = build([
            p('solo'),
            p('orders-clean', { group: 'orders' }),
            p('inv', { group: 'analytics' }),
            p('orders-report', { group: 'orders' }),
        ]);
        expect(show(lines)).toEqual([
            '▸analytics 1 open', '  inv',
            '▸orders 2 open', '  orders-clean', '  orders-report',
            '▸(ungrouped) 1 open', '  solo',
        ]);
        expect(lines.find(l => l.kind === 'group' && l.group === UNGROUPED)).toBeDefined();
    });

    it('trims a group and ignores its case: "Orders" and " orders" are one group', () => {
        const lines = build([p('a', { group: 'Orders' }), p('b', { group: ' orders' }), p('c', { group: 'sales' })]);
        expect(show(lines)).toEqual(['▸Orders 2 open', '  a', '  b', '▸sales 1 open', '  c']);
        expect(groupKey({ group: ' Orders ' })).toBe('orders');
    });

    it('a folded group lists no rows', () => {
        const lines = build([
            p('a', { group: 'orders', status: 'failed' }),
            p('b', { group: 'orders', status: 'timed_out' }),
            p('c', { group: 'orders', status: 'running' }),
        ], '', ['orders']);
        expect(show(lines)).toEqual(['§attention 2', 'a', 'b', '§all 3', '▸orders 3 folded']);
    });

    it('marks the group that holds the open pipeline', () => {
        const groups = (selected: string | null) =>
            buildSidebarLines([p('a', { group: 'g1' }), p('b', { group: 'g2' })], { search: '', folded: [], selected })
                .flatMap(l => (l.kind === 'group' ? [l.current] : []));
        expect(groups('a')).toEqual([true, false]);
        expect(groups(null)).toEqual([false, false]);
    });

    it.each(['constructor', 'toString', '__proto__'])('folds and unfolds a group named %s', group => {
        const pipes = [p('a', { group }), p('b', { group: 'other' })];
        expect(show(build(pipes))).toContain(`▸${group} 1 open`);
        expect(show(build(pipes, '', [groupKey({ group })]))).toContain(`▸${group} 1 folded`);
    });

    it('a search matches names and groups, keeps the groups open, and pins nothing', () => {
        const pipes = [
            p('orders-clean', { group: 'sales', status: 'failed' }),
            p('daily-report', { group: 'orders' }),
            p('inventory', { group: 'stock' }),
        ];
        const lines = build(pipes, 'orders', ['orders', 'sales']);
        expect(show(lines)).toEqual(['§matches 2', '▸orders 1 open', '  daily-report', '▸sales 1 open', '  orders-clean']);
        expect(lines.filter(l => l.kind === 'group').every(l => l.kind === 'group' && !l.foldable)).toBe(true);
    });

    it('a search whose matches have no group lists them flat', () => {
        expect(show(build([p('orders-a'), p('orders-b'), p('sales')], 'orders'))).toEqual(['§matches 2', 'orders-a', 'orders-b']);
    });

    it('a search with no match gives no lines', () => {
        expect(build([p('orders')], 'nothing')).toEqual([]);
    });

    it('gives every line a unique key, also for a pipeline listed twice', () => {
        const lines = build([p('a', { group: 'g', status: 'failed' }), p('b')]);
        const keys = lines.map(l => l.key);
        expect(new Set(keys).size).toBe(keys.length);
    });
});
