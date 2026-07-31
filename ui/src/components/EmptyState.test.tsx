import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Sparkles } from 'lucide-react';
import { EmptyState } from './EmptyState';

describe('EmptyState', () => {
    it('renders the title', () => {
        render(<EmptyState title="Nothing here yet" />);
        expect(screen.getByText('Nothing here yet')).toBeInTheDocument();
    });

    it('renders a description when provided and omits it otherwise', () => {
        const { container, rerender } = render(<EmptyState title="T" description="Some detail" />);
        expect(screen.getByText('Some detail')).toBeInTheDocument();
        rerender(<EmptyState title="T" />);
        expect(container.querySelector('.empty-state__description')).toBeNull();
    });

    it('renders an icon when provided and omits it otherwise', () => {
        const { container, rerender } = render(<EmptyState title="T" icon={Sparkles} />);
        expect(container.querySelector('.empty-state__icon svg')).not.toBeNull();
        rerender(<EmptyState title="T" />);
        expect(container.querySelector('.empty-state__icon')).toBeNull();
    });

    it('renders the action slot when provided and omits it otherwise', () => {
        const { container, rerender } = render(<EmptyState title="T" action={<button>Do it</button>} />);
        expect(screen.getByRole('button', { name: 'Do it' })).toBeInTheDocument();
        rerender(<EmptyState title="T" />);
        expect(container.querySelector('.empty-state__action')).toBeNull();
    });

    it('defaults to a status role and honours an alert override', () => {
        const { rerender } = render(<EmptyState title="T" />);
        expect(screen.getByRole('status')).toBeInTheDocument();
        rerender(<EmptyState title="T" role="alert" />);
        expect(screen.getByRole('alert')).toBeInTheDocument();
    });

    it('applies the tone modifier class for warning / error', () => {
        const { container, rerender } = render(<EmptyState title="Down" tone="warning" />);
        expect(container.querySelector('.empty-state--warning')).toBeInTheDocument();
        rerender(<EmptyState title="Failed" tone="error" />);
        expect(container.querySelector('.empty-state--error')).toBeInTheDocument();
        rerender(<EmptyState title="Neutral" />);
        expect(container.querySelector('.empty-state--warning')).not.toBeInTheDocument();
    });

});
