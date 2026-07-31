'use client';

import React from 'react';
import { logger } from '@/utils/logger';
import { Button } from '@/components/ui/button';
import { AlertTriangle } from '@/utils/icons';

interface ErrorBoundaryProps {
    children: React.ReactNode;
    fallback?: React.ReactNode | ((props: { error: Error | null; reset: () => void }) => React.ReactNode);
    onError?: (error: Error, errorInfo: React.ErrorInfo) => void;
}

interface ErrorBoundaryState {
    hasError: boolean;
    error: Error | null;
    errorInfo: React.ErrorInfo | null;
    errorCount: number;
}

export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
    constructor(props: ErrorBoundaryProps) {
        super(props);
        this.state = { 
            hasError: false, 
            error: null,
            errorInfo: null,
            errorCount: 0
        };
    }
    
    static getDerivedStateFromError(error: Error) {
        return { hasError: true, error };
    }
    
    componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
        logger.error('ErrorBoundary', 'Component error caught', { error, errorInfo });
        
        this.setState(prev => ({
            errorInfo,
            errorCount: prev.errorCount + 1
        }));
        
        this.props.onError?.(error, errorInfo);
    }
    
    handleReset = () => {
        this.setState({ hasError: false, error: null, errorInfo: null });
    };
    
    handleReload = () => {
        window.location.reload();
    };
    
    render() {
        if (this.state.hasError) {
            if (this.props.fallback) {
                return typeof this.props.fallback === 'function'
                    ? this.props.fallback({ 
                        error: this.state.error, 
                        reset: this.handleReset 
                      })
                    : this.props.fallback;
            }
            
            return (
                <div className="eb-error-boundary-container" role="alert">
                    <div className="eb-error-boundary-content">
                        <div className="eb-error-boundary-icon">
                            <AlertTriangle size={48} className="text-amber-500" />
                        </div>
                        <h2 className="eb-error-boundary-title">Something went wrong</h2>
                        <p className="eb-error-boundary-message">
                            {this.state.error?.message || 'An unexpected error occurred'}
                        </p>
                        <div className="eb-error-boundary-actions">
                            <Button variant="secondary" onClick={this.handleReset}>
                                Try Again
                            </Button>
                            <Button onClick={this.handleReload}>
                                Reload Page
                            </Button>
                        </div>
                        {this.state.errorCount > 1 && (
                            <p className="eb-error-boundary-hint">
                                This error has occurred {this.state.errorCount} times. 
                                Try reloading the page.
                            </p>
                        )}
                        <details className="eb-error-boundary-details">
                            <summary>Technical details</summary>
                            <pre className="eb-error-boundary-stack">
                                {this.state.error?.stack}
                            </pre>
                            {this.state.errorInfo?.componentStack && (
                                <pre className="eb-error-boundary-component-stack">
                                    Component Stack:
                                    {this.state.errorInfo.componentStack}
                                </pre>
                            )}
                        </details>
                    </div>
                </div>
            );
        }
        
        return this.props.children;
    }
}

export function withErrorBoundary<P extends Record<string, unknown>>(
    Component: React.ComponentType<P>,
    errorBoundaryProps: Omit<ErrorBoundaryProps, 'children'> = {}
) {
    const WrappedComponent = (props: P) => (
        <ErrorBoundary {...errorBoundaryProps}>
            <Component {...props} />
        </ErrorBoundary>
    );
    
    WrappedComponent.displayName = `withErrorBoundary(${Component.displayName || Component.name || 'Component'})`;
    
    return WrappedComponent;
}

export default ErrorBoundary;
