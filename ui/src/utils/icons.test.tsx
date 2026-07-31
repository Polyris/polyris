/**
 * Icon system invariants (v0.79.7, ADR #79).
 *
 * Locks the single-source-of-truth contract for the backfill icon:
 * ActionIcons.backfill is the one definition; ContextIcons.backfill must
 * alias it (same component reference). This guards against a future edit
 * re-introducing a divergent backfill icon (the bug v0.79.7 fixed, where
 * Rocket / Rewind / History were used in different places).
 */
import { describe, it, expect } from 'vitest';
import { ActionIcons, ContextIcons } from './icons';

describe('icon system SSoT', () => {
    it('exposes a backfill icon on ActionIcons', () => {
        expect(ActionIcons.backfill).toBeDefined();
        expect(typeof ActionIcons.backfill).toBe('object'); // forwardRef component
    });

    it('ContextIcons.backfill is the SAME reference as ActionIcons.backfill', () => {
        // This is the core invariant: one definition, aliased — not two
        // independent icon choices that can drift apart.
        expect(ContextIcons.backfill).toBe(ActionIcons.backfill);
    });

    it('backfill icon is distinct from run icon', () => {
        // Backfill must not silently collapse back into Rocket (the run
        // icon), which was the most common pre-v0.79.7 mistake.
        expect(ActionIcons.backfill).not.toBe(ActionIcons.run);
    });
});
