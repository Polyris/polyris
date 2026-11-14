import { useQuery, useInfiniteQuery } from '@tanstack/react-query';
import { api } from '../../utils';
import { queryKeys } from '../../lib/queryClient';
import type { RunFeedRow, Task } from '../../types';

/** One page of a History feed. `next` is an opaque cursor for the older page. */
interface TasksPage {
    tasks: Task[];
    next: string | null;
}

interface RunsPage {
    runs: RunFeedRow[];
    next: string | null;
}

/**
 * useAllTasksQuery - The History tasks feed, newest first, one page at a time.
 *
 * The API answers with a pageful plus an opaque `next` cursor; `fetchNextPage()`
 * asks for what is older than the last row shown, and `hasNextPage` goes false only
 * once the API says there is nothing older. That is what makes the count honest —
 * the previous single-shot query kept the newest 100 rows and dropped the rest with
 * no way for the UI to know, or to ask for more.
 */
export function useAllTasksQuery(filters: { status?: string; date?: string; pipeline?: string; taskName?: string } = {}, enabled = true) {
    const { status, date, pipeline, taskName } = filters;
    
    return useInfiniteQuery({
        queryKey: queryKeys.allTasks(filters),
        queryFn: async ({ pageParam }): Promise<TasksPage> => {
            const params = new URLSearchParams();
            if (status) params.append('status', status);
            if (date) params.append('date', date);
            if (pipeline) params.append('pipeline', pipeline);
            if (pageParam) params.append('before', pageParam as string);
            
            const data = await api.get(`/tasks?${params.toString()}`);
            if (data.error) {
                throw new Error(data.error);
            }
            
            let tasks: Task[] = data.tasks || [];
            
            // Client-side filter by task name
            if (taskName) {
                const search = taskName.toLowerCase();
                tasks = tasks.filter(t => t.task_name?.toLowerCase().includes(search));
            }
            
            return { tasks, next: (data.next as string) || null };
        },
        initialPageParam: '' as string,
        getNextPageParam: (lastPage) => lastPage.next || undefined,
        enabled,
        staleTime: 5 * 1000,
    });
}

/**
 * useAllRunsQuery - The History runs feed, newest first, one page at a time.
 *
 * Same cursor as the tasks feed (a `started_at`), which is also what lets the merged
 * Run/Activity feed page at all: executions and Backfills interleave by start time
 * and share the one cursor (ADR #95).
 */
export function useAllRunsQuery(date: string, filters: { status?: string; pipeline?: string } = {}, enabled = true) {
    const { status, pipeline } = filters;
    
    return useInfiniteQuery({
        queryKey: queryKeys.allRuns(date, filters),
        queryFn: async ({ pageParam }): Promise<RunsPage> => {
            const params = new URLSearchParams();
            if (date) params.append('date', date);
            if (status) params.append('status', status);
            if (pipeline) params.append('pipeline', pipeline);
            if (pageParam) params.append('before', pageParam as string);
            
            const data = await api.get(`/runs?${params.toString()}`);
            if (data.error) {
                throw new Error(data.error);
            }
            return { runs: (data.runs || []) as RunFeedRow[], next: (data.next as string) || null };
        },
        initialPageParam: '' as string,
        getNextPageParam: (lastPage) => lastPage.next || undefined,
        enabled,
        staleTime: 5 * 1000,
    });
}

/**
 * useTaskEventsQuery - Fetch events for a specific task execution
 */
export function useTaskEventsQuery(executionName: string | null, isModalOpen: boolean) {
    return useQuery({
        queryKey: queryKeys.taskEvents(executionName ?? ''),
        queryFn: async () => {
            const resp = await api.get(`/task-events?name=${encodeURIComponent(executionName!)}`);
            if (resp.error) {
                throw new Error(resp.error);
            }
            return resp.events || [];
        },
        enabled: !!executionName && isModalOpen,
        staleTime: 10 * 1000,
    });
}


/**
 * useNotificationsQuery - Fetch pipeline failure notifications
 * Polls every 30 seconds, returns raw notifications from API
 */
export function useNotificationsQuery(limit = 10, hours = 4) {
    return useQuery({
        queryKey: queryKeys.notifications(limit, hours),
        queryFn: async () => {
            const data = await api.get(`/notifications?limit=${limit}&hours=${hours}`);
            if (data.error) {
                throw new Error(data.error);
            }
            return data.notifications || [];
        },
        staleTime: 15 * 1000,
        refetchInterval: 30 * 1000,
    });
}

const globalQueries = {
    useAllTasksQuery,
    useAllRunsQuery,
    useTaskEventsQuery,
    useNotificationsQuery,
};

export default globalQueries;
