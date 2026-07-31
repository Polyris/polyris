import { useQuery, useInfiniteQuery, keepPreviousData } from '@tanstack/react-query';
import { api } from '../../utils';
import { queryKeys } from '../../lib/queryClient';
import type { Task, DAG, SelectedExecution, Execution, PipelineWithUI } from '../../types';

/** Return type for pipeline detail query */
interface PipelineDetailData {
    tasks: Task[];
    dag: DAG | null;
    serverOffsetMs: number;
    selectedExecution: SelectedExecution | null;
}

/**
 * usePipelinesQuery - Fetch all pipelines with stats
 */
export function usePipelinesQuery(options: { refetchInterval?: number | false } = {}) {
    return useQuery<PipelineWithUI[]>({
        queryKey: queryKeys.pipelines,
        queryFn: async () => {
            const data = await api.get('/pipelines?stats=true');
            if (data.error) {
                throw new Error(data.error);
            }
            return data.pipelines || [];
        },
        staleTime: 10 * 1000, // Pipelines list is stable for 10 seconds
        refetchInterval: options.refetchInterval ?? false,
    });
}

/**
 * usePipelineDetailQuery - Fetch pipeline status, dag, and handle auto-refresh
 */
export function usePipelineDetailQuery(pipelineName: string, date: string, execution: string | null = null, options: { refetchInterval?: number | false } = {}) {
    return useQuery<PipelineDetailData | null>({
        queryKey: queryKeys.pipelineDetail(pipelineName, date, execution),
        queryFn: async (): Promise<PipelineDetailData | null> => {
            if (!pipelineName) return null;
            
            let statusUrl = `/pipeline-status?name=${pipelineName}&date=${date}`;
            let dagUrl = `/pipeline-dag?name=${pipelineName}&date=${date}`;
            
            if (execution) {
                statusUrl += `&pipeline_execution=${encodeURIComponent(execution)}`;
                dagUrl += `&pipeline_execution=${encodeURIComponent(execution)}`;
            }
            
            const [statusData, dagData] = await Promise.all([
                api.get(statusUrl),
                api.get(dagUrl),
            ]);
            
            // Check for race condition
            if (!statusData.error && statusData.pipeline_name && statusData.pipeline_name !== pipelineName) {
                throw new Error('Stale response');
            }
            
            const tasks = statusData.error ? [] : (statusData.tasks || []);
            const serverOffsetMs = statusData.server_now_ms 
                ? statusData.server_now_ms - Date.now() 
                : 0;
            
            const dagObj = dagData.error ? null : (dagData.dag || dagData);
            const dag = dagObj ? {
                nodes: dagObj.nodes || [],
                edges: dagObj.edges || [],
                ...dagObj
            } : null;
            
            // Extract auto-selected execution
            let selectedExecution = null;
            if (!execution && statusData.selected_execution) {
                const fullId = statusData.selected_execution;
                // Prefer the backend-computed short (same value the history list shows). If a
                // task-less execution doesn't carry it, fall back to the same rule as the
                // backend's compute_pipeline_execution_short (last 20 chars, strip '.'/':')
                // so we never render a naive prefix like "hello-wo".
                const shortId = tasks[0]?.pipeline_execution_short
                    || fullId.slice(-20).replace(/[.:]/g, '');
                selectedExecution = {
                    execution_id: fullId,
                    execution_short: shortId,
                    auto_selected: true,
                };
            }
            
            return {
                tasks,
                dag,
                serverOffsetMs,
                selectedExecution,
            };
        },
        enabled: !!pipelineName,
        refetchInterval: options.refetchInterval || false,
        staleTime: 3 * 1000, // Detail data refreshes more frequently
        // Keep the previous date's DAG on screen while a new date loads, so
        // switching dates doesn't flash a skeleton (isLoading stays false).
        placeholderData: keepPreviousData,
    });
}

/**
 * usePipelineExecutionsQuery - Fetch pipeline executions
 */
export function usePipelineExecutionsQuery(pipelineName: string, date: string) {
    return useQuery<Execution[]>({
        queryKey: queryKeys.pipelineExecutions(pipelineName, date),
        queryFn: async () => {
            if (!pipelineName) return [];
            const data = await api.get(`/pipeline-executions?name=${pipelineName}&date=${date}`);
            if (data.error) {
                throw new Error(data.error);
            }
            return data.executions || [];
        },
        enabled: !!pipelineName,
        staleTime: 5 * 1000,
        placeholderData: keepPreviousData,
    });
}


/**
 * usePipelineRunsQuery - Every run of a pipeline, newest first, no date window.
 *
 * Backed by pipeline-date-index: omitting `date` makes the API return runs
 * newest-first with an opaque `next` cursor, rather than only the runs that
 * happen to fall inside a hardcoded day window. How far back you can see is
 * therefore governed by the row TTL alone — raising the TTL deepens history
 * with no code change here.
 *
 * Paginates with useInfiniteQuery: `fetchNextPage()` loads the next (older)
 * page; `hasNextPage` is false once nothing older exists.
 */
export function usePipelineRunsQuery(pipelineName: string, enabled = true) {
    return useInfiniteQuery({
        queryKey: queryKeys.pipelineRuns(pipelineName),
        queryFn: async ({ pageParam }) => {
            const params = new URLSearchParams({ name: pipelineName });
            if (pageParam) params.append('before', pageParam as string);
            const data = await api.get(`/pipeline-executions?${params.toString()}`);
            if (data.error) {
                throw new Error(data.error);
            }
            return { executions: (data.executions || []) as Execution[], next: data.next as string | null };
        },
        initialPageParam: '' as string,
        getNextPageParam: (lastPage) => lastPage.next || undefined,
        enabled: enabled && !!pipelineName,
        staleTime: 5 * 1000,
    });
}


/**
 * useCalendarExecutionsQuery - Fetch executions for a specific month (calendar view)
 */
export function useCalendarExecutionsQuery(pipelineName: string, year: number, month: number) {
    const yearMonth = `${year}-${String(month + 1).padStart(2, '0')}`;
    
    return useQuery({
        queryKey: queryKeys.calendarExecutions(pipelineName, yearMonth),
        queryFn: async () => {
            if (!pipelineName) return [];
            
            const startDate = `${yearMonth}-01`;
            const lastDay = new Date(year, month + 1, 0).getDate();
            const endDate = `${yearMonth}-${String(lastDay).padStart(2, '0')}`;
            
            const data = await api.get(`/pipeline-executions?name=${pipelineName}&start_date=${startDate}&end_date=${endDate}`);
            if (data.error) {
                throw new Error(data.error);
            }
            return data.executions || [];
        },
        enabled: !!pipelineName,
        staleTime: 30 * 1000, // Calendar data stable for 30 seconds
    });
}

/**
 * Lightweight pipeline task list — for selectors like the backfill modal's
 * task-subset picker (ADR #61). Fetches `/pipeline-dag` and projects to
 * `[{task_id, skip_on_backfill?}, ...]`. Cached across modals on the same
 * page (60 sec stale time).
 *
 * Why not reuse `usePipelineDetailQuery`? That hook also loads execution
 * status; for picker purposes only the static task list is needed and we
 * don't want to invalidate on every status refresh.
 */
export interface PipelineTaskRef {
    task_id: string;
    skip_on_backfill?: boolean;
}

export function usePipelineTasksList(pipelineName: string | null) {
    return useQuery<PipelineTaskRef[]>({
        queryKey: queryKeys.pipelineTasksList(pipelineName ?? ''),
        queryFn: async (): Promise<PipelineTaskRef[]> => {
            if (!pipelineName) return [];
            const data = await api.get(`/pipeline-dag?name=${encodeURIComponent(pipelineName)}`);
            if (data.error) {
                throw new Error(data.error);
            }
            const dagObj = data.dag || data;
            const nodes = Array.isArray(dagObj.nodes) ? dagObj.nodes : [];
            return nodes
                .filter((n: { id?: string }) => Boolean(n.id))
                .map((n: { id: string; skip_on_backfill?: boolean }) => ({
                    task_id: n.id,
                    skip_on_backfill: n.skip_on_backfill,
                }));
        },
        enabled: !!pipelineName,
        staleTime: 60 * 1000,
    });
}
