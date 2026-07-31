import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// Minimal modal chrome (open => dialog).
vi.mock('./BaseModal', () => ({
    BaseModal: ({ isOpen, children }: any) => (isOpen ? <div role="dialog">{children}</div> : null),
    ModalHeader: ({ children }: any) => <div>{children}</div>,
    ModalFooter: ({ children }: any) => <div>{children}</div>,
}));

// OSS build: empty paid surface => no Team sections. The free Decision Timeout
// section must still render, so Settings is never an empty shell (ADR #103 1b).
vi.mock('@/ee-active.generated', () => ({ paidSurface: {} }));

// Decision Timeout has its own tests; stub it so this focuses on the container.
vi.mock('./DecisionTimeoutSection', () => ({
    DecisionTimeoutSection: () => <div data-testid="decision-timeout-section">decision</div>,
}));

vi.mock('lucide-react', () => ({
    KeyRound: () => null, Settings: () => null, Bell: () => null, Clock: () => null,
}));

import { SettingsModal } from './SettingsModal';

describe('SettingsModal', () => {
    it('renders nothing when closed', () => {
        const { container } = render(<SettingsModal isOpen={false} onClose={() => {}} />);
        expect(container.querySelector('[role="dialog"]')).toBeNull();
    });

    it('renders the free Decision Timeout section in the OSS build (never an empty shell)', () => {
        render(<SettingsModal isOpen onClose={() => {}} />);
        // The free section is in the nav...
        expect(screen.getByText('Decision Timeout')).toBeInTheDocument();
        // ...and is the default-active section (its body renders).
        expect(screen.getByTestId('decision-timeout-section')).toBeTruthy();
        // No Team-tier sections in the OSS build.
        expect(screen.queryByText('API Tokens')).toBeNull();
        expect(screen.queryByText('Alerts')).toBeNull();
    });
});
