/**
 * Auth Hook Tests (Amplify)
 * 
 * Tests for the authentication context and hooks using AWS Amplify.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import React from 'react';
import { AuthProvider, useAuth, AUTH_STATE } from './useAuth';

// Mock config module - must be mutable so tests can change AUTH.enabled
// (the real config.js freezes the object at import time, so window.CONFIG changes have no effect)
const { mockConfig } = vi.hoisted(() => ({
    mockConfig: {
        AUTH: { enabled: false },
        API_URL: '/api',
        POLL_INTERVAL: 30000,
    },
}));
vi.mock('../lib/config', () => ({ default: mockConfig }));

// Mock Amplify auth functions
vi.mock('aws-amplify/auth', () => ({
    signIn: vi.fn(),
    signOut: vi.fn(),
    getCurrentUser: vi.fn(),
    fetchAuthSession: vi.fn(),
    confirmSignIn: vi.fn(),
    resetPassword: vi.fn(),
    confirmResetPassword: vi.fn(),
    fetchUserAttributes: vi.fn(),
}));

import { 
    signIn as mockSignIn,
    getCurrentUser as mockGetCurrentUser,
    fetchAuthSession as mockFetchAuthSession,
    resetPassword as mockResetPassword,
    confirmResetPassword as mockConfirmResetPassword,
    fetchUserAttributes as mockFetchUserAttributes
} from 'aws-amplify/auth';

// Helper to render hook with provider
const renderAuthHook = () => {
    const wrapper = ({ children }) => (
        <AuthProvider>{children}</AuthProvider>
    );
    return renderHook(() => useAuth(), { wrapper });
};

describe('useAuth Hook (Amplify)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        localStorage.clear();
        
        // Default: auth disabled
        mockConfig.AUTH = { enabled: false };
    });

    afterEach(() => {
        vi.clearAllMocks();
    });

    describe('Auth Disabled', () => {
        it('should show signed in state when auth is disabled', async () => {
            mockConfig.AUTH = { enabled: false };
            
            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN);
            }, { timeout: 1000 });
            
            expect(result.current.isAuthenticated).toBe(true);
            expect(result.current.isAuthEnabled).toBe(false);
        });

        it('should provide anonymous user when auth is disabled', async () => {
            mockConfig.AUTH = { enabled: false };
            
            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.user).toBeDefined();
            }, { timeout: 1000 });
            
            expect(result.current.user.email).toBe('anonymous');
            expect(result.current.user.isAdmin).toBe(true);
        });
    });

    describe('Auth Enabled - Initial State', () => {
        beforeEach(() => {
            mockConfig.AUTH = {
                    enabled: true,
                    userPoolId: 'us-east-1_test',
                    clientId: 'testclient123',
                    region: 'us-east-1'
                };
        });

        it('should show signed out state when no current user', async () => {
            mockGetCurrentUser.mockRejectedValue(new Error('No user'));
            
            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });
            
            expect(result.current.isAuthenticated).toBe(false);
        });

        it('should show signed in state when user exists', async () => {
            mockGetCurrentUser.mockResolvedValue({
                username: 'test@example.com',
                userId: 'user-123',
            });
            mockFetchUserAttributes.mockResolvedValue({
                email: 'test@example.com',
                name: 'Test User',
            });
            mockFetchAuthSession.mockResolvedValue({
                tokens: {
                    accessToken: {
                        payload: { 'cognito:groups': ['admins'] },
                        toString: () => 'mock-token',
                    },
                },
            });
            
            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN);
            }, { timeout: 1000 });
            
            expect(result.current.isAuthenticated).toBe(true);
            expect(result.current.user.email).toBe('test@example.com');
            expect(result.current.user.isAdmin).toBe(true);
        });
    });

    describe('Optimistic init (cached session)', () => {
        const CACHE_KEY = 'polyris-auth-user';
        beforeEach(() => {
            mockConfig.AUTH = { enabled: true, userPoolId: 'us-east-1_test', clientId: 'testclient123', region: 'us-east-1' };
        });

        it('starts signed-in immediately when a session is cached (no loading flash)', () => {
            localStorage.setItem(CACHE_KEY, JSON.stringify({ email: 'test@example.com', name: 'Test User', isAdmin: false }));
            mockGetCurrentUser.mockReturnValue(new Promise(() => {})); // revalidation stays pending
            const { result } = renderAuthHook();
            expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN);
            expect(result.current.isLoading).toBe(false);
            expect(result.current.user.email).toBe('test@example.com');
        });

        it('starts in loading state when no session is cached', () => {
            mockGetCurrentUser.mockReturnValue(new Promise(() => {})); // pending
            const { result } = renderAuthHook();
            expect(result.current.authState).toBe(AUTH_STATE.LOADING);
            expect(result.current.isLoading).toBe(true);
        });

        it('clears the cache and signs out when background revalidation fails', async () => {
            localStorage.setItem(CACHE_KEY, JSON.stringify({ email: 'stale@example.com' }));
            mockGetCurrentUser.mockRejectedValue(new Error('No user'));
            const { result } = renderAuthHook();
            expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN); // optimistic
            await waitFor(() => expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT), { timeout: 1000 });
            expect(localStorage.getItem(CACHE_KEY)).toBeNull();
        });

        it('caches the session after a successful check', async () => {
            mockGetCurrentUser.mockResolvedValue({ username: 'test@example.com', userId: 'user-123' });
            mockFetchUserAttributes.mockResolvedValue({ email: 'test@example.com', name: 'Test User' });
            mockFetchAuthSession.mockResolvedValue({ tokens: { accessToken: { payload: { 'cognito:groups': ['admins'] }, toString: () => 'mock-token' } } });
            const { result } = renderAuthHook();
            await waitFor(() => expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN), { timeout: 1000 });
            expect(localStorage.getItem(CACHE_KEY)).toContain('test@example.com');
        });

        it('clears the cache on sign out', async () => {
            localStorage.setItem(CACHE_KEY, JSON.stringify({ email: 'test@example.com' }));
            mockGetCurrentUser.mockResolvedValue({ username: 'test@example.com', userId: 'user-123' });
            mockFetchUserAttributes.mockResolvedValue({ email: 'test@example.com' });
            mockFetchAuthSession.mockResolvedValue({ tokens: { accessToken: { payload: {}, toString: () => 't' } } });
            const { result } = renderAuthHook();
            await waitFor(() => expect(result.current.authState).toBe(AUTH_STATE.SIGNED_IN), { timeout: 1000 });
            await act(async () => { await result.current.signOut(); });
            expect(localStorage.getItem(CACHE_KEY)).toBeNull();
        });
    });

    describe('Sign In Flow', () => {
        beforeEach(() => {
            mockConfig.AUTH = {
                    enabled: true,
                    userPoolId: 'us-east-1_test',
                    clientId: 'testclient123',
                    region: 'us-east-1'
                };
            mockGetCurrentUser.mockRejectedValue(new Error('No user'));
        });

        it('should handle new password required challenge', async () => {
            mockSignIn.mockResolvedValue({
                nextStep: {
                    signInStep: 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED',
                },
            });

            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });

            await act(async () => {
                const response = await result.current.signIn('test@example.com', 'temppassword');
                expect(response.challenge).toBe('NEW_PASSWORD_REQUIRED');
            });

            expect(result.current.authState).toBe(AUTH_STATE.NEW_PASSWORD_REQUIRED);
        });

        it('should handle invalid credentials error', async () => {
            mockSignIn.mockRejectedValue(new Error('Incorrect username or password.'));

            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });

            await act(async () => {
                try {
                    await result.current.signIn('test@example.com', 'wrongpassword');
                } catch (e) {
                    expect((e as Error).message).toBe('Incorrect username or password.');
                }
            });

            expect(result.current.error).toBe('Incorrect username or password.');
            expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
        });
    });

    describe('Forgot Password', () => {
        beforeEach(() => {
            mockConfig.AUTH = {
                    enabled: true,
                    userPoolId: 'us-east-1_test',
                    clientId: 'testclient123',
                    region: 'us-east-1'
                };
            mockGetCurrentUser.mockRejectedValue(new Error('No user'));
        });

        it('should initiate forgot password flow', async () => {
            mockResetPassword.mockResolvedValue({});

            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });

            await act(async () => {
                const response = await result.current.forgotPassword('test@example.com');
                expect(response.success).toBe(true);
            });
            
            expect(mockResetPassword).toHaveBeenCalledWith({ username: 'test@example.com' });
        });

        it('should confirm forgot password with code', async () => {
            mockConfirmResetPassword.mockResolvedValue({});

            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });

            await act(async () => {
                const response = await result.current.confirmForgotPassword(
                    'test@example.com',
                    '123456',
                    'NewPassword123!'
                );
                expect(response.success).toBe(true);
            });
            
            expect(mockConfirmResetPassword).toHaveBeenCalledWith({
                username: 'test@example.com',
                confirmationCode: '123456',
                newPassword: 'NewPassword123!',
            });
        });
    });

    describe('Error Handling', () => {
        beforeEach(() => {
            mockConfig.AUTH = {
                    enabled: true,
                    userPoolId: 'us-east-1_test',
                    clientId: 'testclient123',
                    region: 'us-east-1'
                };
            mockGetCurrentUser.mockRejectedValue(new Error('No user'));
        });

        it('should clear error on clearError call', async () => {
            mockSignIn.mockRejectedValue(new Error('Some error'));

            const { result } = renderAuthHook();
            
            await waitFor(() => {
                expect(result.current.authState).toBe(AUTH_STATE.SIGNED_OUT);
            }, { timeout: 1000 });

            await act(async () => {
                try {
                    await result.current.signIn('test@example.com', 'password');
                } catch {
                    // expected
                }
            });

            expect(result.current.error).toBe('Some error');

            act(() => {
                result.current.clearError();
            });

            expect(result.current.error).toBe(null);
        });
    });
});
