import { describe, it, expect } from 'vitest';
import { mergeNodePositions } from './reactFlowHelpers';

function node(id: string, x: number, y: number, extra: Record<string, unknown> = {}) {
    return { id, position: { x, y }, ...extra };
}

describe('mergeNodePositions', () => {
    it('keeps the previous position for a node that already existed', () => {
        const fresh = [node('extract', 999, 999)];   // dagre would put it here
        const prev = [node('extract', 10, 20)];       // but the user dragged it here
        const result = mergeNodePositions(fresh, prev);
        expect(result[0].position).toEqual({ x: 10, y: 20 });
    });

    it('uses the fresh position for a node that did not exist before', () => {
        const fresh = [node('new_task', 50, 60)];
        const prev: ReturnType<typeof node>[] = [];
        const result = mergeNodePositions(fresh, prev);
        expect(result[0].position).toEqual({ x: 50, y: 60 });
    });

    it('mixes both: existing nodes keep their position, new ones get the fresh one', () => {
        const fresh = [node('extract', 999, 999), node('brand_new', 5, 5)];
        const prev = [node('extract', 10, 20)];
        const result = mergeNodePositions(fresh, prev);
        expect(result.find(n => n.id === 'extract')!.position).toEqual({ x: 10, y: 20 });
        expect(result.find(n => n.id === 'brand_new')!.position).toEqual({ x: 5, y: 5 });
    });

    it('always takes non-position fields from the fresh node', () => {
        const fresh = [node('extract', 999, 999, { data: { status: 'success' } })];
        const prev = [node('extract', 10, 20, { data: { status: 'running' } })];
        const result = mergeNodePositions(fresh, prev);
        expect(result[0].data).toEqual({ status: 'success' });
        expect(result[0].position).toEqual({ x: 10, y: 20 });
    });

    it('a node that disappeared from the fresh set is simply absent from the result', () => {
        const fresh = [node('extract', 999, 999)];
        const prev = [node('extract', 10, 20), node('removed_task', 1, 1)];
        const result = mergeNodePositions(fresh, prev);
        expect(result.map(n => n.id)).toEqual(['extract']);
    });

    it('first render (no previous nodes) uses all fresh positions unchanged', () => {
        const fresh = [node('a', 1, 1), node('b', 2, 2)];
        const result = mergeNodePositions(fresh, []);
        expect(result).toEqual(fresh);
    });

    it('is idempotent: merging the same inputs twice gives the same result', () => {
        const fresh = [node('extract', 999, 999)];
        const prev = [node('extract', 10, 20)];
        const once = mergeNodePositions(fresh, prev);
        const twice = mergeNodePositions(fresh, prev);
        expect(once).toEqual(twice);
    });

    it('re-merging the merged output against itself is a no-op (stable fixed point)', () => {
        const fresh = [node('extract', 999, 999)];
        const prev = [node('extract', 10, 20)];
        const merged = mergeNodePositions(fresh, prev);
        const remerged = mergeNodePositions(fresh, merged);
        expect(remerged).toEqual(merged);
    });

    it('empty fresh set returns empty, regardless of prev', () => {
        expect(mergeNodePositions([], [node('a', 1, 1)])).toEqual([]);
    });
});
