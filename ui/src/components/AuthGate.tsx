/**
 * AuthGate Component
 * 
 * Wrapper component that shows the login page when user is not authenticated.
 * Also handles:
 * - Loading state during auth initialization
 * - Setting up API auth token getter
 * - Handling 401 errors from API
 * 
 * @module components/AuthGate
 */

import React, { useEffect } from 'react';
import { useAuth } from '@/hooks/useAuth';
import { LoginPage } from './LoginPage';
import { Loader2, Zap } from 'lucide-react';
import { setAuthTokenGetter, setAuthErrorCallback } from '@/utils/api';
import { logger } from '@/utils/logger';

/**
 * Loading screen shown during auth initialization
 */
function AuthLoading() {
    return (
        <div className="ag-auth-loading">
            <div className="ag-auth-loading-content">
                <div className="ag-auth-loading-logo">
                    <Zap size={32} />
                </div>
                <Loader2 size={24} className="animate-spin" />
                <span>Loading...</span>
            </div>
        </div>
    );
}

/**
 * AuthGate - Shows login or app based on auth state
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
    const { 
        isAuthenticated, 
        isLoading, 
        isAuthEnabled,
        getAccessToken,
        signOut 
    } = useAuth();
    
    // Set up API auth integration
    useEffect(() => {
        if (isAuthEnabled) {
            // Set token getter for API requests
            setAuthTokenGetter(getAccessToken);
            
            // Set callback for 401 errors (force sign out)
            setAuthErrorCallback(() => {
                logger.warn('auth', 'API returned 401 - signing out');
                signOut();
            });
        }
        
        return () => {
            setAuthTokenGetter(null);
            setAuthErrorCallback(null);
        };
    }, [isAuthEnabled, getAccessToken, signOut]);
    
    // Show loading during initialization
    if (isLoading) {
        return <AuthLoading />;
    }
    
    // If auth is disabled, show the app directly
    if (!isAuthEnabled) {
        return children;
    }
    
    // Show login page when not authenticated
    if (!isAuthenticated) {
        return <LoginPage />;
    }
    
    // User is authenticated - show the app
    return children;
}


export default AuthGate;
