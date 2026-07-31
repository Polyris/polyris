'use client';

import { logger } from '@/utils/logger';
import { asStringArray } from '@/utils/formatters';

/**
 * Auth Context and Hooks using AWS Amplify
 * 
 * This module provides authentication state management for the polyris Console
 * using AWS Amplify Auth library with Cognito User Pool.
 * 
 * Features:
 * - Secure password authentication (SRP)
 * - Automatic token refresh (handled by Amplify)
 * - Session persistence
 * - Force password change flow (for new users)
 * - MFA support (TOTP)
 * 
 * Usage:
 *   // In main.jsx, wrap app with AuthProvider
 *   <AuthProvider>
 *     <App />
 *   </AuthProvider>
 * 
 *   // In components, use the useAuth hook
 *   const { user, isAuthenticated, signIn, signOut } = useAuth();
 * 
 * @module hooks/useAuth
 */

import React, { createContext, useContext, useState, useEffect, useLayoutEffect, useCallback } from 'react';
import { 
    signIn as amplifySignIn,
    signOut as amplifySignOut,
    getCurrentUser,
    fetchAuthSession,
    confirmSignIn,
    resetPassword,
    confirmResetPassword,
    fetchUserAttributes
} from 'aws-amplify/auth';
import config from '../lib/config';

/** Extract error message safely from unknown catch value */
function getErrorMessage(err: unknown): string {
    if (err instanceof Error) return err.message;
    return String(err);
}

// -----------------------------------------------------------------------------
// Constants
// -----------------------------------------------------------------------------

// Auth states
export const AUTH_STATE = {
    LOADING: 'loading',
    SIGNED_IN: 'signed_in',
    SIGNED_OUT: 'signed_out',
    NEW_PASSWORD_REQUIRED: 'new_password_required',
    MFA_REQUIRED: 'mfa_required',
    MFA_SETUP: 'mfa_setup',
};

// -----------------------------------------------------------------------------
// Helpers
// -----------------------------------------------------------------------------

/**
 * Check if auth is enabled.
 *
 * Delegates to the single resolver in lib/config (which reads window.CONFIG
 * first and falls back to NEXT_PUBLIC_AUTH_ENABLED via `??`). This module used
 * to re-implement the check with a `!== undefined` guard, which treated a
 * `null` in config.js (the local-dev "fall through to .env" sentinel) as
 * "defined but not true" and silently disabled auth — diverging from config.ts.
 * One resolver, one behaviour.
 */
const isAuthEnabled = () => config.AUTH?.enabled === true;

/**
 * Extract user info from Amplify user object
 */
const formatUser = (amplifyUser: { username?: string } | null, attributes: Partial<Record<string, string>> = {}) => {
    if (!amplifyUser) return null;
    
    const email = attributes.email || amplifyUser.username;
    
    return {
        username: amplifyUser.username,
        email: email,
        name: attributes.name || email?.split('@')[0],
        emailVerified: attributes.email_verified === 'true',
    };
};

/**
 * Optimistic-init cache.
 *
 * The formatted user from the last successful auth check is cached in localStorage so the
 * provider can render as signed-in immediately on the next mount (a static export re-mounts
 * this provider on navigation), instead of flashing a loading screen while Amplify's async
 * session check runs. The cached value is UI-only and always revalidated in the background —
 * the server validates every request, so a stale/revoked token simply 401s and signs out.
 */
const AUTH_CACHE_KEY = 'polyris-auth-user';

const readCachedAuth = (): AuthUser | null => {
    try {
        const raw = localStorage.getItem(AUTH_CACHE_KEY);
        return raw ? (JSON.parse(raw) as AuthUser) : null;
    } catch {
        return null;
    }
};

const writeCachedAuth = (user: AuthUser): void => {
    try { localStorage.setItem(AUTH_CACHE_KEY, JSON.stringify(user)); } catch { /* ignore */ }
};

const clearCachedAuth = (): void => {
    try { localStorage.removeItem(AUTH_CACHE_KEY); } catch { /* ignore */ }
};

/**
 * useLayoutEffect on the client, useEffect on the server. The optimistic cache read must run
 * before the first paint (so the loading splash isn't visible when a session is cached), but
 * it must NOT change the initial render — the static export prerenders the LOADING state, so
 * reading localStorage in the initial state would cause a hydration mismatch. Running the read
 * in a layout effect keeps the first render server-identical and upgrades before paint.
 */
const useIsoLayoutEffect = typeof window !== 'undefined' ? useLayoutEffect : useEffect;

// -----------------------------------------------------------------------------
// Auth Context
// -----------------------------------------------------------------------------

/** Formatted user info */
export interface AuthUser {
    username?: string;
    email?: string;
    name?: string;
    emailVerified?: boolean;
    isAdmin?: boolean;
    [key: string]: unknown;
}

interface AuthContextValue {
    authState: string;
    isAuthenticated: boolean;
    isLoading: boolean;
    isAuthEnabled: boolean;
    user: AuthUser | null;
    error: string | null;
    challengeData?: Record<string, unknown> | null;
    signIn: (email: string, password: string) => Promise<Record<string, unknown>>;
    signOut: () => Promise<void>;
    completeNewPassword: (newPassword: string, attributes?: Record<string, unknown>) => Promise<Record<string, unknown>>;
    verifyMfa: (code: string) => Promise<Record<string, unknown>>;
    forgotPassword: (email: string) => Promise<Record<string, unknown>>;
    confirmForgotPassword: (email: string, code: string, newPassword: string) => Promise<Record<string, unknown>>;
    getAccessToken: () => Promise<string | null>;
    clearError: () => void;
    refreshAuth: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/**
 * Auth Provider Component
 * 
 * Provides authentication state and methods to child components.
 * Uses Amplify Auth for session management and token refresh.
 */
export function AuthProvider({ children }: { children: React.ReactNode }) {
    const authEnabled = isAuthEnabled();
    // Server-safe initial state that matches the prerendered LOADING splash. The optimistic
    // upgrade from the cached session happens in the layout effect below (hydration-safe).
    const [user, setUser] = useState<AuthUser | null>(null);
    const [authState, setAuthState] = useState(AUTH_STATE.LOADING);
    const [error, setError] = useState<string | null>(null);
    const [challengeUser, setChallengeUser] = useState<unknown>(null);
    
    // -------------------------------------------------------------------------
    // Check Current Auth State
    // -------------------------------------------------------------------------
    
    const checkAuthState = useCallback(async () => {
        if (!authEnabled) {
            setUser({ email: 'anonymous', name: 'Anonymous', isAdmin: true });
            setAuthState(AUTH_STATE.SIGNED_IN);
            return;
        }
        
        try {
            const currentUser = await getCurrentUser();
            const attributes: Partial<Record<string, string>> = await fetchUserAttributes();
            const session = await fetchAuthSession();
            
            // Extract groups from token
            const groups = asStringArray(session.tokens?.accessToken?.payload?.['cognito:groups']);
            
            const formattedUser = {
                ...formatUser(currentUser, attributes),
                groups,
                isAdmin: groups.includes('admins'),
            };
            
            setUser(formattedUser);
            setAuthState(AUTH_STATE.SIGNED_IN);
            writeCachedAuth(formattedUser);
        } catch {
            // Not authenticated — drop any optimistic cache so we don't render a ghost session.
            clearCachedAuth();
            setUser(null);
            setAuthState(AUTH_STATE.SIGNED_OUT);
        }
    }, [authEnabled]);
    
    // Optimistic upgrade from the cached session, before first paint. Kept out of the initial
    // render (see the server-safe useState above) so hydration stays consistent with the
    // prerendered LOADING splash; checkAuthState() below then revalidates in the background.
    useIsoLayoutEffect(() => {
        if (!authEnabled) return;
        const cached = readCachedAuth();
        if (cached) {
            setUser(cached);
            setAuthState(AUTH_STATE.SIGNED_IN);
        }
    }, [authEnabled]);

    // Initialize auth state on mount
    useEffect(() => {
        checkAuthState(); // eslint-disable-line react-hooks/set-state-in-effect -- One-time initialization from external auth system
    }, [checkAuthState]);
    
    // -------------------------------------------------------------------------
    // Authentication Methods
    // -------------------------------------------------------------------------
    
    /**
     * Sign in with email and password
     */
    const signIn = useCallback(async (email: string, password: string) => {
        setError(null);
        
        try {
            const result = await amplifySignIn({
                username: email,
                password: password,
            });
            
            // Check for challenges
            if (result.nextStep) {
                const { signInStep } = result.nextStep;
                
                if (signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED') {
                    setChallengeUser(result);
                    setAuthState(AUTH_STATE.NEW_PASSWORD_REQUIRED);
                    return { challenge: 'NEW_PASSWORD_REQUIRED' };
                }
                
                if (signInStep === 'CONFIRM_SIGN_IN_WITH_TOTP_CODE') {
                    setChallengeUser(result);
                    setAuthState(AUTH_STATE.MFA_REQUIRED);
                    return { challenge: 'MFA_REQUIRED' };
                }
                
                if (signInStep === 'CONTINUE_SIGN_IN_WITH_TOTP_SETUP') {
                    setChallengeUser(result);
                    setAuthState(AUTH_STATE.MFA_SETUP);
                    return { challenge: 'MFA_SETUP' };
                }
                
                if (signInStep === 'DONE') {
                    await checkAuthState();
                    return { success: true, user };
                }
            }
            
            // Sign in complete
            await checkAuthState();
            return { success: true, user };
            
        } catch (err: unknown) {
            // Handle stale session - sign out and retry once
            const errMsg = getErrorMessage(err);
            if (errMsg.includes('already') && errMsg.includes('signed in')) {
                try {
                    await amplifySignOut();
                    const retryResult = await amplifySignIn({
                        username: email,
                        password: password,
                    });
                    if (retryResult.nextStep) {
                        const { signInStep } = retryResult.nextStep;
                        if (signInStep === 'CONFIRM_SIGN_IN_WITH_NEW_PASSWORD_REQUIRED') {
                            setChallengeUser(retryResult);
                            setAuthState(AUTH_STATE.NEW_PASSWORD_REQUIRED);
                            return { challenge: 'NEW_PASSWORD_REQUIRED' };
                        }
                        if (signInStep === 'CONFIRM_SIGN_IN_WITH_TOTP_CODE') {
                            setChallengeUser(retryResult);
                            setAuthState(AUTH_STATE.MFA_REQUIRED);
                            return { challenge: 'MFA_REQUIRED' };
                        }
                        if (signInStep === 'DONE') {
                            await checkAuthState();
                            return { success: true, user };
                        }
                    }
                    await checkAuthState();
                    return { success: true, user };
                } catch (retryErr: unknown) {
                    const retryMessage = getErrorMessage(retryErr) || 'Sign in failed after clearing session';
                    setError(retryMessage);
                    throw new Error(retryMessage);
                }
            }
            const errorMessage = getErrorMessage(err) || 'Sign in failed';
            setError(errorMessage);
            throw new Error(errorMessage);
        }
    }, [checkAuthState, user]);
    
    /**
     * Complete new password challenge
     */
    const completeNewPassword = useCallback(async (newPassword: string) => {
        if (!challengeUser) {
            throw new Error('No active challenge');
        }
        
        setError(null);
        
        try {
            const result = await confirmSignIn({
                challengeResponse: newPassword,
            });
            
            // Check for additional challenges (like MFA setup)
            if (result.nextStep?.signInStep === 'CONTINUE_SIGN_IN_WITH_TOTP_SETUP') {
                setChallengeUser(result);
                setAuthState(AUTH_STATE.MFA_SETUP);
                return { challenge: 'MFA_SETUP' };
            }
            
            if (result.nextStep?.signInStep === 'DONE') {
                setChallengeUser(null);
                await checkAuthState();
                return { success: true };
            }
            
            setChallengeUser(null);
            await checkAuthState();
            return { success: true };
            
        } catch (err: unknown) {
            const errorMessage = getErrorMessage(err) || 'Password change failed';
            setError(errorMessage);
            throw new Error(errorMessage);
        }
    }, [challengeUser, checkAuthState]);
    
    /**
     * Verify MFA code
     */
    const verifyMfa = useCallback(async (code: string) => {
        if (!challengeUser) {
            throw new Error('No active MFA challenge');
        }
        
        setError(null);
        
        try {
            const result = await confirmSignIn({
                challengeResponse: code,
            });
            
            if (result.nextStep?.signInStep === 'DONE') {
                setChallengeUser(null);
                await checkAuthState();
                return { success: true };
            }
            
            setChallengeUser(null);
            await checkAuthState();
            return { success: true };
            
        } catch (err: unknown) {
            const errorMessage = getErrorMessage(err) || 'MFA verification failed';
            setError(errorMessage);
            throw new Error(errorMessage);
        }
    }, [challengeUser, checkAuthState]);
    
    /**
     * Sign out
     */
    const signOut = useCallback(async () => {
        try {
            // Local sign-out only — clears THIS browser's tokens without a
            // server-side GlobalSignOut. Global revocation here was revoking the
            // refresh token on every sign-out (including the automatic one fired
            // on a 401), so the next session restore failed with "Access Token
            // has been revoked" (400) and login appeared broken. See CHANGELOG
            // v0.89.4. (A deliberate "sign out everywhere" can be added later as
            // a separate, explicit action.)
            await amplifySignOut();
        } catch (err: unknown) {
            logger.warn('auth', 'Sign out error', err);
        }
        
        clearCachedAuth();
        setUser(null);
        setChallengeUser(null);
        setError(null);
        setAuthState(AUTH_STATE.SIGNED_OUT);
    }, []);
    
    /**
     * Get current access token
     */
    const getAccessToken = useCallback(async () => {
        if (!authEnabled) {
            return null;
        }
        
        try {
            const session = await fetchAuthSession();
            return session.tokens?.accessToken?.toString() || null;
        } catch (err: unknown) {
            logger.warn('auth', 'Failed to get access token', err);
            return null;
        }
    }, [authEnabled]);
    
    /**
     * Forgot password - initiate reset
     */
    const forgotPassword = useCallback(async (email: string) => {
        setError(null);
        
        try {
            await resetPassword({ username: email });
            return { success: true };
        } catch (err: unknown) {
            const errorMessage = getErrorMessage(err) || 'Password reset failed';
            setError(errorMessage);
            throw new Error(errorMessage);
        }
    }, []);
    
    /**
     * Confirm forgot password with code
     */
    const confirmForgotPassword = useCallback(async (email: string, code: string, newPassword: string) => {
        setError(null);
        
        try {
            await confirmResetPassword({
                username: email,
                confirmationCode: code,
                newPassword: newPassword,
            });
            return { success: true };
        } catch (err: unknown) {
            const errorMessage = getErrorMessage(err) || 'Password reset confirmation failed';
            setError(errorMessage);
            throw new Error(errorMessage);
        }
    }, []);
    
    // -------------------------------------------------------------------------
    // Context Value
    // -------------------------------------------------------------------------
    
    const value: AuthContextValue = {
        // State
        authState,
        isAuthenticated: authState === AUTH_STATE.SIGNED_IN,
        isLoading: authState === AUTH_STATE.LOADING,
        isAuthEnabled: authEnabled,
        user,
        error,
        
        // Methods
        signIn,
        signOut,
        completeNewPassword,
        verifyMfa,
        forgotPassword,
        confirmForgotPassword,
        getAccessToken,
        clearError: () => setError(null),
        refreshAuth: checkAuthState,
    };
    
    return (
        <AuthContext.Provider value={value}>
            {children}
        </AuthContext.Provider>
    );
}

/**
 * Hook to access auth context
 */
export function useAuth(): AuthContextValue {
    const context = useContext(AuthContext);
    if (!context) {
        throw new Error('useAuth must be used within an AuthProvider');
    }
    return context;
}

/**
 * Hook to get auth header for API requests
 */
export function useAuthHeader() {
    const { getAccessToken, isAuthEnabled } = useAuth();
    
    return useCallback(async () => {
        if (!isAuthEnabled) {
            return {};
        }
        
        const token = await getAccessToken();
        if (!token) {
            return {};
        }
        
        return {
            Authorization: `Bearer ${token}`,
        };
    }, [getAccessToken, isAuthEnabled]);
}

export default AuthContext;
