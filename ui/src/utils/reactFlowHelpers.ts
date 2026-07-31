/**
 * React Flow node-position preservation.
 *
 * A graph's *structure* (dagre layout) and a node's *current position* are different
 * things once a user has dragged a node — the layout still says where a node would go
 * by default, but the node itself may be sitting somewhere else on purpose. Rebuilding
 * the node list on every data refresh (a status poll, a new event) must not throw that
 * away: the fresh render carries fresh statuses/labels, `mergeNodePositions` is what
 * carries the position forward for any node that already existed.
 *
 * Idempotent by construction: merging the same `freshNodes` against the same `prevNodes`
 * twice produces the same result both times — there is no accumulating state, only a
 * lookup against whatever `prevNodes` currently holds.
 */

interface PositionedNode {
    id: string;
    position: { x: number; y: number };
}

/**
 * Return `freshNodes` with each node's `position` replaced by its previous position,
 * for any node whose id already existed in `prevNodes`. A node with no previous match
 * (brand new, e.g. a newly-deployed task) keeps the fresh (dagre-computed) position.
 *
 * Every other field (status, label, data, …) always comes from `freshNodes` — only
 * `position` is carried over. Edges need no equivalent: they have no position of their
 * own, they just connect two node ids.
 *
 * `prevNodes` is typed structurally on purpose (just `id` + `position`, not the same
 * `T` as `freshNodes`): React Flow's own `Node<TData>` type carries a slightly wider
 * `type: string | undefined` than a fresh node literal's `type: 'task'`, and this
 * function never returns an element *from* `prevNodes` — it only reads `id`/`position`
 * off it — so requiring an exact type match there would reject the real caller for no
 * behavioural reason.
 */
export function mergeNodePositions<T extends PositionedNode>(
    freshNodes: T[],
    prevNodes: readonly PositionedNode[]
): T[] {
    if (prevNodes.length === 0) return freshNodes;   // nothing to preserve on the first render
    const prevById = new Map(prevNodes.map(n => [n.id, n]));
    return freshNodes.map(fresh => {
        const existing = prevById.get(fresh.id);
        return existing ? { ...fresh, position: existing.position } : fresh;
    });
}
