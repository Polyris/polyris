import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { 
    usePipelinesQuery,
    usePipelineDetailQuery,
    usePipelineExecutionsQuery,
} from './usePipelineQueries';

// Mock api
vi.mock('../../utils', () => ({
    api: {
        get: vi.fn(),
        post: vi.fn(),
    },
}));

import { api } from '../../utils';

// Create wrapper with fresh QueryClient for each test
const createWrapper = () => {
    const queryClient = new QueryClient({
        defaultOptions: {
            queries: {
                retry: false,
                gcTime: 0,
            },
        },
    });
    
    return function Wrapper({ children }) {
        return (
            <QueryClientProvider client={queryClient}>
                {children}
            </QueryClientProvider>
        );
    };
};

describe('usePipelinesQuery', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('should fetch pipelines successfully', async () => {
        const mockPipelines = [
            { name: 'pipeline-1', status: 'running' },
            { name: 'pipeline-2', status: 'success' },
        ];
        api.get.mockResolvedValue({ pipelines: mockPipelines });
        
        const { result } = renderHook(() => usePipelinesQuery(), {
            wrapper: createWrapper(),
        });
        
        await waitFor(() => {
            expect(result.current.isSuccess).toBe(true);
        });
        
        expect(api.get).toHaveBeenCalledWith('/pipelines?stats=true');
        expect(result.current.data).toEqual(mockPipelines);
    });

    it('should handle API error', async () => {
        api.get.mockResolvedValue({ error: 'Connection failed' });
        
        const { result } = renderHook(() => usePipelinesQuery(), {
            wrapper: createWrapper(),
        });
        
        await waitFor(() => {
            expect(result.current.isError).toBe(true);
        });
        
        expect(result.current.error.message).toBe('Connection failed');
    });

    it('should return empty array when no pipelines', async () => {
        api.get.mockResolvedValue({});
        
        const { result } = renderHook(() => usePipelinesQuery(), {
            wrapper: createWrapper(),
        });
        
        await waitFor(() => {
            expect(result.current.isSuccess).toBe(true);
        });
        
        expect(result.current.data).toEqual([]);
    });
});

describe('usePipelineDetailQuery', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('should fetch pipeline details', async () => {
        const mockStatus = {
            pipeline_name: 'test-pipeline',
            tasks: [{ task_name: 'task-1', status: 'running' }],
            server_now_ms: Date.now(),
        };
        const mockDag = {
            nodes: [{ id: 'task-1' }],
            edges: [],
        };
        
        api.get.mockImplementation((url) => {
            if (url.includes('pipeline-status')) return Promise.resolve(mockStatus);
            if (url.includes('pipeline-dag')) return Promise.resolve({ dag: mockDag });
            return Promise.resolve({});
        });
        
        const { result } = renderHook(
            () => usePipelineDetailQuery('test-pipeline', '2024-01-15'),
            { wrapper: createWrapper() }
        );
        
        await waitFor(() => {
            expect(result.current.isSuccess).toBe(true);
        });
        
        expect(result.current.data.tasks).toEqual(mockStatus.tasks);
        expect(result.current.data.dag).toEqual(mockDag);
    });

    it('uses backend pipeline_execution_short for the auto-selected execution', async () => {
        const mockStatus = {
            pipeline_name: 'test-pipeline',
            selected_execution: 'test-pipeline-2024-01-15-7a6e479e',
            tasks: [{ task_name: 'task-1', status: 'succeeded', pipeline_execution_short: '2024-01-15-7a6e479e' }],
            server_now_ms: Date.now(),
        };
        api.get.mockImplementation((url) => {
            if (url.includes('pipeline-status')) return Promise.resolve(mockStatus);
            if (url.includes('pipeline-dag')) return Promise.resolve({ dag: { nodes: [], edges: [] } });
            return Promise.resolve({});
        });

        const { result } = renderHook(
            () => usePipelineDetailQuery('test-pipeline', '2024-01-15'),
            { wrapper: createWrapper() }
        );

        await waitFor(() => expect(result.current.isSuccess).toBe(true));
        expect(result.current.data.selectedExecution).toMatchObject({
            execution_short: '2024-01-15-7a6e479e',
            auto_selected: true,
        });
    });

    it('falls back to a clean short (no phantom prefix) when the execution has no tasks', async () => {
        const mockStatus = {
            pipeline_name: 'hello-world',
            selected_execution: 'hello-world-2024-01-15-7a6e479e',
            tasks: [],
            server_now_ms: Date.now(),
        };
        api.get.mockImplementation((url) => {
            if (url.includes('pipeline-status')) return Promise.resolve(mockStatus);
            if (url.includes('pipeline-dag')) return Promise.resolve({ dag: { nodes: [], edges: [] } });
            return Promise.resolve({});
        });

        const { result } = renderHook(
            () => usePipelineDetailQuery('hello-world', '2024-01-15'),
            { wrapper: createWrapper() }
        );

        await waitFor(() => expect(result.current.isSuccess).toBe(true));
        const short = result.current.data.selectedExecution.execution_short;
        expect(short).not.toContain('hello');
        expect(short).toBe('-2024-01-15-7a6e479e');
    });

    it('should not fetch when pipelineName is empty', () => {
        const { result } = renderHook(
            () => usePipelineDetailQuery('', '2024-01-15'),
            { wrapper: createWrapper() }
        );
        
        expect(result.current.fetchStatus).toBe('idle');
        expect(api.get).not.toHaveBeenCalled();
    });

    it('should include execution parameter when provided', async () => {
        api.get.mockResolvedValue({ tasks: [], dag: { nodes: [], edges: [] } });
        
        renderHook(
            () => usePipelineDetailQuery('test-pipeline', '2024-01-15', 'exec-123'),
            { wrapper: createWrapper() }
        );
        
        await waitFor(() => {
            expect(api.get).toHaveBeenCalledWith(
                expect.stringContaining('pipeline_execution=exec-123')
            );
        });
    });
});

describe('usePipelineExecutionsQuery', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('should fetch executions', async () => {
        const mockExecutions = [
            { execution_id: 'exec-1', status: 'running' },
            { execution_id: 'exec-2', status: 'success' },
        ];
        api.get.mockResolvedValue({ executions: mockExecutions });
        
        const { result } = renderHook(
            () => usePipelineExecutionsQuery('test-pipeline', '2024-01-15'),
            { wrapper: createWrapper() }
        );
        
        await waitFor(() => {
            expect(result.current.isSuccess).toBe(true);
        });
        
        expect(api.get).toHaveBeenCalledWith('/pipeline-executions?name=test-pipeline&date=2024-01-15');
        expect(result.current.data).toEqual(mockExecutions);
    });
});
