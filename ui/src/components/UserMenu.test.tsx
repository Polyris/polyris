import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { UserMenu } from './UserMenu';
import { useAppStore } from '@/stores/useAppStore';

// Mutable auth state so each test can toggle authenticated vs no-auth.
const { authState } = vi.hoisted(() => ({
    authState: {
        user: { name: 'Test User', email: 'test@test.com', isAdmin: false } as
            | { name: string; email: string; isAdmin: boolean }
            | null,
        signOut: vi.fn(),
        isAuthEnabled: true,
    },
}));

vi.mock('@/hooks/useAuth', () => ({ useAuth: () => authState }));
vi.mock('./SettingsModal', () => ({ SettingsModal: () => null }));

describe('UserMenu', () => {
    beforeEach(() => {
        useAppStore.getState().setShowHelpModal(false);
        authState.user = { name: 'Test User', email: 'test@test.com', isAdmin: false };
        authState.isAuthEnabled = true;
    });

    it('opens Help & documentation from the dropdown (authenticated)', () => {
        render(<UserMenu />);
        fireEvent.click(screen.getByRole('button', { name: /Test User/i }));
        fireEvent.click(screen.getByText(/Help & documentation/i));
        expect(useAppStore.getState().showHelpModal).toBe(true);
    });

    it('toggles the theme from the dark mode switch', () => {
        useAppStore.getState().setTheme('light');
        render(<UserMenu />);
        fireEvent.click(screen.getByRole('button', { name: /Test User/i }));
        fireEvent.click(screen.getByRole('menuitemcheckbox', { name: /dark mode/i }));
        expect(useAppStore.getState().theme).toBe('dark');
    });

    it('keeps Settings and Help reachable with no Sign Out when auth is disabled', () => {
        authState.user = null;
        authState.isAuthEnabled = false;
        render(<UserMenu />);
        fireEvent.click(screen.getByRole('button', { name: /Menu/i }));
        expect(screen.getByText('Settings')).toBeInTheDocument();
        expect(screen.getByText(/Help & documentation/i)).toBeInTheDocument();
        expect(screen.queryByText('Sign Out')).not.toBeInTheDocument();
    });
});
