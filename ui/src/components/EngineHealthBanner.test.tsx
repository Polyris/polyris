import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import EngineHealthBanner from './EngineHealthBanner';

const health = { current: { problems: [] as string[] } };

vi.mock('@/utils/icons', () => ({ AlertTriangle: () => null }));
vi.mock('@/hooks/queries', () => ({
    useEngineHealthQuery: () => ({ data: health.current }),
}));

describe('EngineHealthBanner', () => {
    beforeEach(() => {
        health.current = { problems: [] };
    });

    it('renders nothing while the engine is healthy', () => {
        const { container } = render(<EngineHealthBanner />);
        expect(container).toBeEmptyDOMElement();
    });

    it('lists every problem as an alert while the engine is unhealthy', () => {
        health.current = { problems: ['The sweeper last reported 20 minutes ago.', 'Waiting tasks are 60 minutes behind.'] };
        render(<EngineHealthBanner />);
        expect(screen.getByRole('alert')).toHaveTextContent('Task coordination needs attention.');
        expect(screen.getByText('The sweeper last reported 20 minutes ago.')).toBeInTheDocument();
        expect(screen.getByText('Waiting tasks are 60 minutes behind.')).toBeInTheDocument();
    });
});
