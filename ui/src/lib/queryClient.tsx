'use client';

import React from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

/**
 * Query client configuration
 */
export const queryClient = new QueryClient({
    defaultOptions: {
        queries: {
            staleTime: 5 * 1000,
            gcTime: 5 * 60 * 1000,
            retry: 1,
            refetchOnWindowFocus: true,
            refetchOnReconnect: true,
        },
        mutations: {
            retry: 0,
        },
    },
});

/**
 * Query keys factory for consistent key management
 */
export const queryKeys = {
    // Deployment entitlement surface (ADR #100) — fixed per deployment.
    capabilities: ['capabilities'] as const,
    pipelines: ['pipelines'] as const,
    pipelineDetail: (name: string, date: string, execution: string | null) => ['pipeline', name, date, execution] as const,
    pipelineExecutions: (name: string, date: string) => ['pipeline', name, 'executions', date] as const,
    pipelineRuns: (name: string) => ['pipeline', name, 'runs'] as const,
    calendarExecutions: (name: string, yearMonth: string) => ['pipeline', name, 'calendar', yearMonth] as const,
    
    allTasks: (filters: Record<string, unknown>) => ['tasks', filters] as const,
    taskEvents: (executionName: string) => ['task', executionName, 'events'] as const,
    
    allRuns: (date: string, filters: Record<string, unknown>) => ['runs', date, filters] as const,
    
    consecutiveProgress: (waitFor: unknown, date: string) => ['assets', 'consecutive-progress', waitFor, date] as const,
    
    // Asset queries
    assetsData: () => ['assets', 'data'] as const,
    assetEvents: (name: string) => ['assets', 'events', name] as const,
    assetGlueSchema: (name: string) => ['assets', 'glue-schema', name] as const,
    assetMatrix: (from: string, to: string, group: string | null, includeViews: boolean, granularity: string = 'daily') =>
        ['assets', 'matrix', from, to, group ?? '', includeViews, granularity] as const,
    assetDrift: () => ['assets', 'drift'] as const,
    
    notifications: (limit: number, hours: number) => ['notifications', limit, hours] as const,
    
    globalStats: ['global', 'stats'] as const,

    // Backfill queries (v0.78+, per ADR #51)
    backfillsList: (status: string | null) => ['backfills', 'list', status ?? ''] as const,
    backfillDetail: (backfillId: string) => ['backfill', backfillId] as const,

    // Lightweight pipeline task list for selectors (e.g. backfill modal).
    // Differs from pipelineDetail (which fetches status + executions). This
    // hits /pipeline-dag and extracts task IDs only — cached across modals.
    pipelineTasksList: (pipelineName: string) => ['pipeline', pipelineName, 'tasks-list'] as const,

    // Global decision-wait timeout (ADR #103 1b) — one value per deployment.
    decisionTimeout: ['settings', 'decision-timeout'] as const,
};

/**
 * React Query Provider component
 */
export function QueryProvider({ children }: { children: React.ReactNode }) {
    return (
        <QueryClientProvider client={queryClient}>
            {children}
        </QueryClientProvider>
    );
}

export default QueryProvider;
