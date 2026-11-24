import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ActionModal } from './ActionModal';

function modal(action: string) {
    return {
        isOpen: true, title: 'Restart Task', message: null, icon: null, confirmText: 'Restart', action,
    };
}

function renderModal(action: string, onWithDependantsChange = vi.fn()) {
    render(
        <ActionModal
            modal={modal(action)}
            onClose={vi.fn()}
            onConfirm={vi.fn()}
            loading={false}
            pipelineName="orders"
            triggerParams="{}"
            onTriggerParamsChange={vi.fn()}
            withDependants={false}
            onWithDependantsChange={onWithDependantsChange}
        />,
    );
    return onWithDependantsChange;
}

describe('ActionModal — Restart with dependants (ADR #137 §3)', () => {
    it('offers the dependants on a Restart and says when they are ticked', () => {
        const onChange = renderModal('restart');
        fireEvent.click(screen.getByRole('checkbox', { name: /rerun the dependants/i }));
        expect(onChange).toHaveBeenCalledWith(true);
    });

    it('offers them on no other action', () => {
        renderModal('skip');
        expect(screen.queryByRole('checkbox')).toBeNull();
    });
});
