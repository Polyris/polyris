import React from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { LoginPage } from './LoginPage';

// ─── Mocks ──────────────────────────────────────────────────────────────────
const mockSignIn = vi.fn();
const mockClearError = vi.fn();
const mockCompleteNewPassword = vi.fn();
const mockVerifyMfa = vi.fn();
const mockForgotPassword = vi.fn();
const mockConfirmForgotPassword = vi.fn();

let mockAuthState = 'signedOut';
let mockError: string | null = null;

vi.mock('@/hooks/useAuth', () => ({
    useAuth: () => ({
        authState: mockAuthState,
        signIn: mockSignIn,
        clearError: mockClearError,
        error: mockError,
        completeNewPassword: mockCompleteNewPassword,
        verifyMfa: mockVerifyMfa,
        forgotPassword: mockForgotPassword,
        confirmForgotPassword: mockConfirmForgotPassword,
        challengeData: null,
    }),
    AUTH_STATE: {
        SIGNED_IN: 'signedIn',
        SIGNED_OUT: 'signedOut',
        NEW_PASSWORD_REQUIRED: 'newPasswordRequired',
        MFA_REQUIRED: 'mfaRequired',
        MFA_SETUP: 'mfaSetup',
    },
}));

vi.mock('lucide-react', () => ({
    Activity: () => null, AlertCircle: (p: Record<string, unknown>) => <span data-testid="alert-icon">{p.children as React.ReactNode}</span>,
    ArrowLeft: () => null, Check: () => null, CheckCircle: () => null,
    ChevronDown: () => null, ChevronRight: () => null, ChevronUp: () => null,
    Circle: () => null, Eye: () => <span data-testid="eye-icon" />,
    EyeOff: () => <span data-testid="eye-off-icon" />, HelpCircle: () => null,
    KeyRound: () => null, ListTodo: () => null,
    Loader2: () => <span data-testid="loader" />, Lock: () => null,
    LogOut: () => null, Mail: () => null, Menu: () => null,
    Moon: () => null, Package: () => null, Pause: () => null,
    RefreshCw: () => null, Shield: () => null, Sun: () => null,
    User: () => null, Users: () => null, Workflow: () => null,
    X: () => null, Zap: () => null,
}));

vi.mock('@/components/ui/button', () => ({
    Button: (props: Record<string, unknown>) => (
        <button
            onClick={props.onClick as React.MouseEventHandler}
            disabled={props.disabled as boolean}
            type={(props.type as string) || 'button'}
            className={props.className as string}
            data-variant={props.variant as string}
        >
            {props.children as React.ReactNode}
        </button>
    ),
}));

vi.mock('@/components/ui/input', () => ({
    Input: (props: React.InputHTMLAttributes<HTMLInputElement>) => (
        <input
            id={props.id}
            type={props.type}
            value={props.value}
            onChange={props.onChange}
            placeholder={props.placeholder}
            required={props.required}
            disabled={props.disabled}
            minLength={props.minLength}
            autoFocus={props.autoFocus}
            maxLength={props.maxLength}
            pattern={props.pattern}
        />
    ),
}));

describe('LoginPage', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        mockAuthState = 'signedOut';
        mockError = null;
    });

    // ─── Layout ─────────────────────────────────────────────────────────

    describe('layout', () => {
        it('renders polyris branding', () => {
            render(<LoginPage />);
            expect(screen.getByText('polyris')).toBeInTheDocument();
            expect(screen.getByText('Console')).toBeInTheDocument();
        });

        it('renders sign-in subtitle', () => {
            render(<LoginPage />);
            expect(screen.getByText('Sign in to manage your data pipelines')).toBeInTheDocument();
        });

        it('renders footer', () => {
            render(<LoginPage />);
            expect(screen.getByText('Serverless Data Pipeline Orchestration')).toBeInTheDocument();
        });
    });

    // ─── Login Form ─────────────────────────────────────────────────────

    describe('login form', () => {
        it('renders email and password fields', () => {
            render(<LoginPage />);
            expect(screen.getByPlaceholderText('you@company.com')).toBeInTheDocument();
            expect(screen.getByPlaceholderText('••••••••••••')).toBeInTheDocument();
        });

        it('renders sign in button', () => {
            render(<LoginPage />);
            expect(screen.getByText('Sign In')).toBeInTheDocument();
        });

        it('renders forgot password link', () => {
            render(<LoginPage />);
            expect(screen.getByText('Forgot your password?')).toBeInTheDocument();
        });

        it('calls signIn on form submit', async () => {
            mockSignIn.mockResolvedValue(undefined);
            render(<LoginPage />);

            fireEvent.change(screen.getByPlaceholderText('you@company.com'), { target: { value: 'test@example.com' } });
            fireEvent.change(screen.getByPlaceholderText('••••••••••••'), { target: { value: 'password123' } });
            fireEvent.click(screen.getByText('Sign In'));

            await waitFor(() => {
                expect(mockSignIn).toHaveBeenCalledWith('test@example.com', 'password123');
            });
        });

        it('clears error on submit', async () => {
            mockSignIn.mockResolvedValue(undefined);
            render(<LoginPage />);

            fireEvent.change(screen.getByPlaceholderText('you@company.com'), { target: { value: 'a@b.com' } });
            fireEvent.change(screen.getByPlaceholderText('••••••••••••'), { target: { value: 'pass' } });
            fireEvent.click(screen.getByText('Sign In'));

            expect(mockClearError).toHaveBeenCalled();
        });

        it('displays error message when error is set', () => {
            mockError = 'Invalid credentials';
            render(<LoginPage />);
            expect(screen.getByText('Invalid credentials')).toBeInTheDocument();
        });
    });

    // ─── Password Visibility Toggle ─────────────────────────────────────

    describe('password visibility', () => {
        it('toggles password field type on eye icon click', () => {
            render(<LoginPage />);
            const passwordInput = screen.getByPlaceholderText('••••••••••••');
            expect(passwordInput).toHaveAttribute('type', 'password');

            // Find and click the toggle button
            const toggleButton = document.querySelector('.lp-password-toggle');
            if (toggleButton) {
                fireEvent.click(toggleButton);
                expect(passwordInput).toHaveAttribute('type', 'text');
            }
        });
    });

    // ─── Forgot Password Flow ───────────────────────────────────────────

    describe('forgot password', () => {
        it('switches to forgot password form when link clicked', () => {
            render(<LoginPage />);
            fireEvent.click(screen.getByText('Forgot your password?'));
            expect(screen.getByText('Reset Password')).toBeInTheDocument();
        });

        it('has back button in forgot password form', () => {
            render(<LoginPage />);
            fireEvent.click(screen.getByText('Forgot your password?'));
            expect(screen.getByText('Back to Sign In')).toBeInTheDocument();
        });

        it('returns to login form when back clicked', () => {
            render(<LoginPage />);
            fireEvent.click(screen.getByText('Forgot your password?'));
            fireEvent.click(screen.getByText('Back to Sign In'));
            expect(screen.getByText('Sign In')).toBeInTheDocument();
        });
    });

    // ─── Auth State Routing ─────────────────────────────────────────────

    describe('auth state routing', () => {
        it('shows new password form when auth state is newPasswordRequired', () => {
            mockAuthState = 'newPasswordRequired';
            render(<LoginPage />);
            expect(screen.getByText('Set Your Password')).toBeInTheDocument();
        });

        it('shows MFA form when auth state is mfaRequired', () => {
            mockAuthState = 'mfaRequired';
            render(<LoginPage />);
            expect(screen.getByText('Two-Factor Authentication')).toBeInTheDocument();
        });
    });
});
