import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { RefreshButton } from './RefreshButton';

describe('RefreshButton', () => {
    it('calls onRefresh when clicked', () => {
        const onRefresh = vi.fn();
        render(<RefreshButton onRefresh={onRefresh} />);
        fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
        expect(onRefresh).toHaveBeenCalledTimes(1);
    });

    it('renders an optional label', () => {
        render(<RefreshButton onRefresh={() => {}} label="Refresh" />);
        expect(screen.getByText('Refresh')).toBeInTheDocument();
    });

    it('is disabled and does not fire while fetching', () => {
        const onRefresh = vi.fn();
        render(<RefreshButton onRefresh={onRefresh} isFetching />);
        const btn = screen.getByRole('button', { name: 'Refresh' });
        expect(btn).toBeDisabled();
        fireEvent.click(btn);
        expect(onRefresh).not.toHaveBeenCalled();
    });

    it('spins the icon only while fetching', () => {
        const { container, rerender } = render(<RefreshButton onRefresh={() => {}} isFetching={false} />);
        expect(container.querySelector('.animate-spin')).not.toBeInTheDocument();
        rerender(<RefreshButton onRefresh={() => {}} isFetching />);
        expect(container.querySelector('.animate-spin')).toBeInTheDocument();
    });
});
