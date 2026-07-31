'use client';

import React from 'react';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { ToastProvider } from '@/components/Toast';
import { QueryProvider } from '@/lib/queryClient';
import { AuthProvider } from '@/hooks/useAuth';
import { AuthGate } from '@/components/AuthGate';
import { RouteProvider } from '@/hooks/useClientRoute';
import { configureAmplify } from '@/lib/amplifyConfig';

// Configure Amplify before rendering (runs once on client)
if (typeof window !== 'undefined') {
    configureAmplify();
}

export function Providers({ children }: { children: React.ReactNode }) {
    return (
        <ErrorBoundary>
            <RouteProvider>
                <AuthProvider>
                    <QueryProvider>
                        <ToastProvider maxToasts={5}>
                            <AuthGate>
                                {children}
                            </AuthGate>
                        </ToastProvider>
                    </QueryProvider>
                </AuthProvider>
            </RouteProvider>
        </ErrorBoundary>
    );
}
