import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useTaskEvents } from './useTaskEvents';

// Mock api
vi.mock('../utils', () => ({
  api: {
    get: vi.fn(),
  },
}));

import { api } from '../utils';

describe('useTaskEvents', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('initial state', () => {
    it('should return empty events and not loading initially', () => {
      const { result } = renderHook(() => 
        useTaskEvents(null, false)
      );
      
      expect(result.current.events).toEqual([]);
      expect(result.current.loading).toBe(false);
    });

    it('should not fetch when modal is closed', () => {
      renderHook(() => useTaskEvents('task-123', false));
      
      expect(api.get).not.toHaveBeenCalled();
    });

    it('should not fetch when executionName is null', () => {
      renderHook(() => useTaskEvents(null, true));
      
      expect(api.get).not.toHaveBeenCalled();
    });
  });

  describe('fetching events', () => {
    it('should fetch events when modal opens with execution name', async () => {
      const mockEvents = [
        { id: 1, type: 'TaskStarted', timestamp: '2024-01-01T00:00:00Z' },
        { id: 2, type: 'TaskCompleted', timestamp: '2024-01-01T00:01:00Z' },
      ];
      
      api.get.mockResolvedValue({ events: mockEvents });
      
      const { result } = renderHook(() => 
        useTaskEvents('task-execution-123', true)
      );
      
      expect(result.current.loading).toBe(true);
      
      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });
      
      expect(result.current.events).toEqual(mockEvents);
      expect(api.get).toHaveBeenCalledWith('/task-events?name=task-execution-123');
    });

    it('should encode execution name in URL', async () => {
      api.get.mockResolvedValue({ events: [] });
      
      renderHook(() => useTaskEvents('task:with:colons', true));
      
      await waitFor(() => {
        expect(api.get).toHaveBeenCalledWith('/task-events?name=task%3Awith%3Acolons');
      });
    });

    it('should handle empty response', async () => {
      api.get.mockResolvedValue({});
      
      const { result } = renderHook(() => 
        useTaskEvents('task-123', true)
      );
      
      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });
      
      expect(result.current.events).toEqual([]);
    });

    it('should handle API error gracefully', async () => {
      api.get.mockRejectedValue(new Error('Network error'));
      
      const { result } = renderHook(() => 
        useTaskEvents('task-123', true)
      );
      
      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });
      
      expect(result.current.events).toEqual([]);
    });
  });

  describe('refetching behavior', () => {
    it('should refetch when execution name changes', async () => {
      api.get.mockResolvedValue({ events: [{ id: 1 }] });
      
      const { result, rerender } = renderHook(
        ({ execName }) => useTaskEvents(execName, true),
        { initialProps: { execName: 'task-1' } }
      );
      
      await waitFor(() => {
        expect(result.current.events).toEqual([{ id: 1 }]);
      });
      
      api.get.mockResolvedValue({ events: [{ id: 2 }] });
      
      rerender({ execName: 'task-2' });
      
      await waitFor(() => {
        expect(result.current.events).toEqual([{ id: 2 }]);
      });
      
      expect(api.get).toHaveBeenCalledTimes(2);
    });

    it('should refetch when modal reopens', async () => {
      api.get.mockResolvedValue({ events: [] });
      
      const { result, rerender } = renderHook(
        ({ isOpen }) => useTaskEvents('task-1', isOpen),
        { initialProps: { isOpen: true } }
      );
      
      await waitFor(() => {
        expect(result.current.loading).toBe(false);
      });
      
      // Close modal
      rerender({ isOpen: false });
      
      // Reopen modal
      rerender({ isOpen: true });
      
      await waitFor(() => {
        expect(api.get).toHaveBeenCalledTimes(2);
      });
    });
  });
});
