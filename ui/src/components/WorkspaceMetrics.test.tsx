import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { WorkspaceMetrics, WorkspaceFilterChips } from './WorkspaceMetrics';
import { fireEvent } from '@testing-library/react';

describe('WorkspaceMetrics', () => {
    it('renders each metric value and label', () => {
        render(<WorkspaceMetrics metrics={[
            { label: 'Total', value: 12 },
            { label: 'Failed', value: 3, tone: 'failed' },
        ]} />);
        expect(screen.getByText('12')).toBeInTheDocument();
        expect(screen.getByText('Total')).toBeInTheDocument();
        expect(screen.getByText('3')).toBeInTheDocument();
    });

    it('applies the tone modifier class', () => {
        const { container } = render(<WorkspaceMetrics metrics={[{ label: 'Running', value: 1, tone: 'running' }]} />);
        expect(container.querySelector('.ws-metric--running')).toBeInTheDocument();
    });

    it('renders nothing when there are no metrics', () => {
        const { container } = render(<WorkspaceMetrics metrics={[]} />);
        expect(container.querySelector('.ws-metrics')).not.toBeInTheDocument();
    });
});

describe('WorkspaceFilterChips', () => {
    it('renders a chip per filter and removes on click', () => {
        const onRemove = vi.fn();
        render(<WorkspaceFilterChips chips={[{ label: 'Status', value: 'failed', onRemove }]} />);
        expect(screen.getByText('failed')).toBeInTheDocument();
        fireEvent.click(screen.getByTitle('Remove Status filter'));
        expect(onRemove).toHaveBeenCalledOnce();
    });

    it('renders nothing when no chips', () => {
        const { container } = render(<WorkspaceFilterChips chips={[]} />);
        expect(container.querySelector('.ws-filter-chips')).not.toBeInTheDocument();
    });
});
