import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import React from 'react';
import { renderHook, act } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { usePipelineActions } from './usePipelineActions';

// Mock api and dagHelpers
vi.mock('@/utils', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
  getUpstreamTasks: vi.fn(),
  getDownstreamTasks: vi.fn(),
  getApiErrorMessage: (result: { error?: string; detail?: string }) =>
    result.detail ? `${result.error}: ${result.detail}` : (result.error ?? 'Unknown error'),
}));

// Mock icons
vi.mock('@/utils/icons', () => ({
  Rocket: () => 'RocketIcon',
  StopCircle: () => 'StopCircleIcon',
  SkipForward: () => 'SkipForwardIcon',
  Pause: () => 'PauseIcon',
  XCircle: () => 'XCircleIcon',
  RotateCcw: () => 'RotateCcwIcon',
  Target: () => 'TargetIcon',
  Play: () => 'PlayIcon',
  CircleDot: () => 'CircleDotIcon',
  CheckCircle2: () => 'CheckCircle2Icon',
}));

import { api, getUpstreamTasks, getDownstreamTasks } from '@/utils';

const mockGetUpstream = getUpstreamTasks as ReturnType<typeof vi.fn>;
const mockGetDownstream = getDownstreamTasks as ReturnType<typeof vi.fn>;
const mockApiPost = api.post as ReturnType<typeof vi.fn>;

function createWrapper() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: queryClient }, children);
  }
  return Wrapper;
}

describe('usePipelineActions', () => {
  const defaultProps = {
    selectedPipeline: { name: 'test-pipeline' } as { name: string },
    selectedTask: { task_name: 'task-1', execution_name: 'exec-task-1' } as { task_name: string; execution_name: string },
    selectedExecution: { execution_id: 'exec-123' } as { execution_id: string },
    executions: [{ execution_id: 'exec-123', status: 'running' }] as Array<{ execution_id: string; status: string; execution_arn?: string }>,
    tasks: [
      { task_name: 'task-1', status: 'running', execution_name: 'exec-task-1' },
      { task_name: 'task-2', status: 'pending', execution_name: 'exec-task-2' },
    ] as Array<{ task_name: string; status: string; execution_name: string }>,
    dag: {
      nodes: [{ id: 'task-1' }, { id: 'task-2' }, { id: 'task-3' }],
      edges: [{ from: 'task-1', to: 'task-2' }],
    } as { nodes: Array<{ id: string }>; edges: Array<{ from: string; to: string }> },
    date: '2024-01-15',
    setDate: vi.fn(),
    setSelectedExecution: vi.fn(),
    showToast: vi.fn(),
    onSelectTask: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    mockGetUpstream.mockReturnValue([]);
    mockGetDownstream.mockReturnValue([]);
  });

  describe('initial state', () => {
    it('should initialize with closed modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      expect(result.current.modal.isOpen).toBe(false);
      expect(result.current.triggerParams).toBe('{}');
      expect(result.current.executionPaused).toBe(false);
    });
  });

  describe('handleRun', () => {
    beforeEach(() => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date('2026-07-22T12:00:00Z'));
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('should open run modal defaulting to today, not the currently-viewed date', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      expect(result.current.modal.isOpen).toBe(true);
      expect(result.current.modal.action).toBe('runPipeline');
      expect(result.current.modal.title).toBe('Run Pipeline');
      // defaultProps.date is '2024-01-15' (simulating a stale/historical
      // date left over from browsing) — Run must ignore it and use today.
      expect(result.current.triggerParams).toBe(JSON.stringify({ current_date: '2026-07-22' }, null, 2));
    });

    it('should not open modal without selected pipeline', () => {
      const props = { ...defaultProps, selectedPipeline: null };
      const { result } = renderHook(() => usePipelineActions(props), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      expect(result.current.modal.isOpen).toBe(false);
    });
  });

  describe('handleStop', () => {
    it('should open stop confirmation modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleStop();
      });
      
      expect(result.current.modal.isOpen).toBe(true);
      expect(result.current.modal.action).toBe('stopPipeline');
      expect(result.current.modal.title).toBe('Stop Pipeline');
      expect(result.current.modal.message).toContain('test-pipeline');
    });
  });

  describe('handleTaskAction', () => {
    it('should open skip task modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleTaskAction('skip');
      });
      
      expect(result.current.modal.isOpen).toBe(true);
      expect(result.current.modal.action).toBe('skip');
      expect(result.current.modal.title).toBe('Skip Task');
    });

    it('should open fail task modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleTaskAction('fail');
      });
      
      expect(result.current.modal.action).toBe('fail');
      expect(result.current.modal.title).toBe('Mark Failed');
    });

    it('should open restart task modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleTaskAction('restart');
      });
      
      expect(result.current.modal.action).toBe('restart');
      expect(result.current.modal.title).toBe('Restart Task');
    });

    it('should use provided task instead of selected', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      const customTask = { task_name: 'custom-task' } as { task_name: string };
      
      act(() => {
        result.current.handleTaskAction('skip', customTask);
      });
      
      expect(result.current.modal.message).toContain('custom-task');
    });
  });

  describe('handleRunAction (partial runs)', () => {
    it('runToHere should calculate upstream tasks', () => {
      mockGetUpstream.mockReturnValue(['task-1']);
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      const task = { task_name: 'task-2' } as { task_name: string };
      
      act(() => {
        result.current.handleRunAction('toHere', task);
      });
      
      expect(result.current.modal.title).toBe('Run to "task-2"');
      expect(result.current.modal.toRun).toContain('task-1');
      expect(result.current.modal.toRun).toContain('task-2');
    });

    it('runFromHere should calculate downstream tasks', () => {
      mockGetDownstream.mockReturnValue(['task-3']);
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      const task = { task_name: 'task-2' } as { task_name: string };
      
      act(() => {
        result.current.handleRunAction('fromHere', task);
      });
      
      expect(result.current.modal.title).toBe('Run from "task-2"');
      expect(result.current.modal.toRun).toContain('task-2');
      expect(result.current.modal.toRun).toContain('task-3');
    });

    it('runOnlyThis should run single task', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      const task = { task_name: 'task-2' } as { task_name: string };
      
      act(() => {
        result.current.handleRunAction('onlyThis', task);
      });
      
      expect(result.current.modal.title).toBe('Run only "task-2"');
      expect(result.current.modal.toRun).toEqual(['task-2']);
      expect(result.current.modal.toSkip).toContain('task-1');
      expect(result.current.modal.toSkip).toContain('task-3');
    });
  });

  describe('executeModalAction', () => {
    it('should run pipeline and show success toast', async () => {
      mockApiPost.mockResolvedValue({ execution_arn: 'arn:aws:states:exec-new' });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      await act(async () => {
        await result.current.executeModalAction();
      });
      
      expect(mockApiPost).toHaveBeenCalledWith(
        '/pipeline-run?name=test-pipeline',
        expect.objectContaining({ input: expect.any(Object) })
      );
      expect(defaultProps.showToast).toHaveBeenCalledWith('Pipeline started!', 'success');
    });

    it('should handle run error', async () => {
      mockApiPost.mockResolvedValue({ error: 'Pipeline already running' });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      await act(async () => {
        await result.current.executeModalAction();
      });
      
      expect(defaultProps.showToast).toHaveBeenCalledWith('Pipeline already running', 'error');
    });

    it('should stop all incomplete tasks when stopping pipeline', async () => {
      mockApiPost.mockResolvedValue({ success: true });
      
      const props = {
        ...defaultProps,
        tasks: [
          { task_name: 't1', status: 'running', execution_name: 'exec-t1' },
          { task_name: 't2', status: 'success', execution_name: 'exec-t2' },
          { task_name: 't3', status: 'pending', execution_name: 'exec-t3' },
        ] as Array<{ task_name: string; status: string; execution_name: string }>,
      };
      
      const { result } = renderHook(() => usePipelineActions(props), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleStop();
      });
      
      await act(async () => {
        await result.current.executeModalAction();
      });
      
      // Should stop running and pending, not success
      expect(mockApiPost).toHaveBeenCalledWith('/task-stop?name=exec-t1', {});
      expect(mockApiPost).toHaveBeenCalledWith('/task-stop?name=exec-t3', {});
      expect(mockApiPost).not.toHaveBeenCalledWith('/task-stop?name=exec-t2', {});
    });

    it('should execute skip task action', async () => {
      mockApiPost.mockResolvedValue({ success: true });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleTaskAction('skip');
      });
      
      await act(async () => {
        await result.current.executeModalAction();
      });
      
      expect(mockApiPost).toHaveBeenCalledWith('/task-skip?name=exec-task-1', { date: '2024-01-15' });
      expect(defaultProps.showToast).toHaveBeenCalledWith('Task skipped', 'success');
    });

    it('should handle invalid JSON params', async () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
        result.current.setTriggerParams('{ invalid json }');
      });
      
      await act(async () => {
        await result.current.executeModalAction();
      });
      
      expect(defaultProps.showToast).toHaveBeenCalledWith(
        expect.stringContaining('Invalid JSON'),
        'error'
      );
    });

    it('should close modal on action "close"', async () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      act(() => {
        result.current.closeModal();
      });
      
      expect(result.current.modal.isOpen).toBe(false);
    });
  });

  describe('handlePauseResume', () => {
    it('should pause running execution', async () => {
      mockApiPost.mockResolvedValue({ success: true });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      await act(async () => {
        await result.current.handlePauseResume();
      });
      
      expect(mockApiPost).toHaveBeenCalledWith('/execution-pause?id=exec-123', {});
      expect(defaultProps.showToast).toHaveBeenCalledWith(
        expect.stringContaining('Pipeline paused'),
        'info'
      );
      expect(result.current.executionPaused).toBe(true);
    });

    it('should resume paused execution', async () => {
      mockApiPost.mockResolvedValue({ success: true });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      // First pause
      await act(async () => {
        await result.current.handlePauseResume();
      });
      
      // Then resume
      await act(async () => {
        await result.current.handlePauseResume();
      });
      
      expect(mockApiPost).toHaveBeenLastCalledWith('/execution-resume?id=exec-123', {});
      expect(result.current.executionPaused).toBe(false);
    });

    it('should show warning when no active execution', async () => {
      const props = { ...defaultProps, executions: [] as typeof defaultProps.executions };
      const { result } = renderHook(() => usePipelineActions(props), { wrapper: createWrapper() });
      
      await act(async () => {
        await result.current.handlePauseResume();
      });
      
      expect(defaultProps.showToast).toHaveBeenCalledWith('No active execution to pause', 'warning');
    });

    it('targets the SELECTED execution, not a different running one that happens to appear earlier in the array', async () => {
      // Regression test for a real bug: the previous lookup was a single
      // .find() with an OR condition (selected-match OR running-match), with
      // no priority between the two branches — whichever an element
      // satisfied first in array order won. A user viewing an older,
      // selected execution while a different, more recent one is running
      // (and sorted earlier, as is typical) would have Pause silently act
      // on the wrong execution.
      mockApiPost.mockResolvedValue({ success: true });
      const props = {
        ...defaultProps,
        selectedExecution: { execution_id: 'yesterday-run-1' },
        executions: [
          { execution_id: 'today-run-2', status: 'running' },   // different execution, running, earlier in array
          { execution_id: 'yesterday-run-1', status: 'success' }, // the one the user actually selected
        ],
      };
      const { result } = renderHook(() => usePipelineActions(props), { wrapper: createWrapper() });

      await act(async () => {
        await result.current.handlePauseResume();
      });

      expect(mockApiPost).toHaveBeenCalledWith('/execution-pause?id=yesterday-run-1', {});
    });
  });

  // handleBackfill removed in v0.78 (ADR #51) — backfill now flows through
  // the universal openBackfillModal store action instead of a callback in
  // usePipelineActions. BackfillModal owns the API call.

  describe('handleRefresh', () => {
    it('should invalidate queries (does not crash)', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRefresh();
      });
      
      // handleRefresh now uses queryClient.invalidateQueries — no direct calls to assert
      // Just verify it doesn't throw
    });

    it('should not crash without selected pipeline', () => {
      const props = { ...defaultProps, selectedPipeline: null };
      const { result } = renderHook(() => usePipelineActions(props), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRefresh();
      });
      
      // Should not throw
    });
  });

  describe('handleExtendPause', () => {
    it('should extend pause for current execution', async () => {
      mockApiPost.mockResolvedValue({ success: true });
      
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      await act(async () => {
        await result.current.handleExtendPause();
      });
      
      expect(mockApiPost).toHaveBeenCalledWith('/execution-extend?id=exec-123', {});
      expect(defaultProps.showToast).toHaveBeenCalledWith('Pause extended by 12 hours', 'success');
    });
  });

  describe('closeModal', () => {
    it('should close modal', () => {
      const { result } = renderHook(() => usePipelineActions(defaultProps), { wrapper: createWrapper() });
      
      act(() => {
        result.current.handleRun();
      });
      
      expect(result.current.modal.isOpen).toBe(true);
      
      act(() => {
        result.current.closeModal();
      });
      
      expect(result.current.modal.isOpen).toBe(false);
    });
  });
});
