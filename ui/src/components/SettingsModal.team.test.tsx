import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('./BaseModal', () => ({
    BaseModal: ({ isOpen, children }: any) => (isOpen ? <div role="dialog">{children}</div> : null),
    ModalHeader: ({ children }: any) => <div>{children}</div>,
    ModalFooter: ({ children }: any) => <div>{children}</div>,
}));

// Team build: the paid surface supplies the Team sections. They must appear
// alongside the free Decision Timeout section (which stays present and default).
vi.mock('@/ee-active.generated', () => ({
    paidSurface: {
        ApiTokensSection: () => <div data-testid="tokens-section">tokens</div>,
        AlertsSection: () => <div data-testid="alerts-section">alerts</div>,
    },
}));

vi.mock('./DecisionTimeoutSection', () => ({
    DecisionTimeoutSection: () => <div data-testid="decision-timeout-section">decision</div>,
}));

vi.mock('lucide-react', () => ({
    KeyRound: () => null, Settings: () => null, Bell: () => null, Clock: () => null,
}));

import { SettingsModal } from './SettingsModal';

describe('SettingsModal (Team)', () => {
    it('shows the free Decision Timeout plus the Team sections', () => {
        render(<SettingsModal isOpen onClose={() => {}} />);
        // Free section is default-active (body renders).
        expect(screen.getByTestId('decision-timeout-section')).toBeTruthy();
        // Team sections are wired into the nav.
        expect(screen.getByText('API Tokens')).toBeInTheDocument();
        expect(screen.getByText('Alerts')).toBeInTheDocument();
        // Free section's nav entry is also present.
        expect(screen.getByText('Decision Timeout')).toBeInTheDocument();
    });
});
