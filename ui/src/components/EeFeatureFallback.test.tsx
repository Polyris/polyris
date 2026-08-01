import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { EeFeatureFallback } from './EeFeatureFallback';

describe('EeFeatureFallback', () => {
    it('names the feature and explains it is gated to the paid tier', () => {
        render(<EeFeatureFallback feature="Backfills" />);
        expect(screen.getByText(/Backfills isn.t available in this edition/i)).toBeInTheDocument();
        expect(screen.getByText('This feature isn’t available in this build.')).toBeInTheDocument();
    });

    it('omits the home button when no onHome handler is provided', () => {
        render(<EeFeatureFallback feature="Backfills" />);
        expect(screen.queryByRole('button')).toBeNull();
    });

    it('fires the home button when onHome is provided', () => {
        const onHome = vi.fn();
        render(<EeFeatureFallback feature="Backfills" onHome={onHome} />);
        fireEvent.click(screen.getByText('Go to Pipelines'));
        expect(onHome).toHaveBeenCalledTimes(1);
    });
});
