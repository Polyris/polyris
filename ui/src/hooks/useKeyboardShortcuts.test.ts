import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useKeyboardShortcuts, SHORTCUTS } from './useKeyboardShortcuts';

describe('useKeyboardShortcuts', () => {
  let handlers;
  
  beforeEach(() => {
    handlers = {
      search: vi.fn(),
      help: vi.fn(),
      escape: vi.fn(),
      refresh: vi.fn(),
    };
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  const fireKeyDown = (options) => {
    const event = new KeyboardEvent('keydown', {
      bubbles: true,
      cancelable: true,
      ...options,
    });
    document.dispatchEvent(event);
  };

  describe('basic shortcuts', () => {
    it('should call handler on matching shortcut', () => {
      renderHook(() => useKeyboardShortcuts({
        'escape': handlers.escape,
      }));
      
      fireKeyDown({ key: 'Escape' });
      
      expect(handlers.escape).toHaveBeenCalledTimes(1);
    });

    it('should handle ctrl+key shortcuts', () => {
      renderHook(() => useKeyboardShortcuts({
        'ctrl+k': handlers.search,
      }));
      
      fireKeyDown({ key: 'k', ctrlKey: true });
      
      expect(handlers.search).toHaveBeenCalledTimes(1);
    });

    it('should handle meta+key (cmd) shortcuts', () => {
      renderHook(() => useKeyboardShortcuts({
        'ctrl+k': handlers.search,
      }));
      
      fireKeyDown({ key: 'k', metaKey: true });
      
      expect(handlers.search).toHaveBeenCalledTimes(1);
    });

    it('should handle shift modifier', () => {
      renderHook(() => useKeyboardShortcuts({
        'ctrl+shift+r': handlers.refresh,
      }));
      
      fireKeyDown({ key: 'r', ctrlKey: true, shiftKey: true });
      
      expect(handlers.refresh).toHaveBeenCalledTimes(1);
    });

    it('should handle ? shortcut (shift+/)', () => {
      renderHook(() => useKeyboardShortcuts({
        '?': handlers.help,
      }));
      
      fireKeyDown({ key: '/', shiftKey: true });
      
      expect(handlers.help).toHaveBeenCalledTimes(1);
    });

    it('should not call handler for non-matching shortcut', () => {
      renderHook(() => useKeyboardShortcuts({
        'ctrl+k': handlers.search,
      }));
      
      fireKeyDown({ key: 'j', ctrlKey: true });
      
      expect(handlers.search).not.toHaveBeenCalled();
    });

    it('should not call handler without required modifier', () => {
      renderHook(() => useKeyboardShortcuts({
        'ctrl+k': handlers.search,
      }));
      
      fireKeyDown({ key: 'k' }); // No ctrl
      
      expect(handlers.search).not.toHaveBeenCalled();
    });
  });

  describe('input ignoring', () => {
    it('should ignore shortcuts when typing in input by default', () => {
      renderHook(() => useKeyboardShortcuts({
        'k': handlers.search,
      }));
      
      // Create an input element
      const input = document.createElement('input');
      document.body.appendChild(input);
      input.focus();
      
      // Simulate keydown on input
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        bubbles: true,
      });
      Object.defineProperty(event, 'target', { value: input });
      document.dispatchEvent(event);
      
      expect(handlers.search).not.toHaveBeenCalled();
      
      document.body.removeChild(input);
    });

    it('should still handle Escape in inputs', () => {
      renderHook(() => useKeyboardShortcuts({
        'escape': handlers.escape,
      }));
      
      const input = document.createElement('input');
      document.body.appendChild(input);
      input.focus();
      
      const event = new KeyboardEvent('keydown', {
        key: 'Escape',
        bubbles: true,
      });
      Object.defineProperty(event, 'target', { value: input });
      document.dispatchEvent(event);
      
      expect(handlers.escape).toHaveBeenCalled();
      
      document.body.removeChild(input);
    });

    it('should not ignore inputs when ignoreInputs is false', () => {
      renderHook(() => useKeyboardShortcuts(
        { 'k': handlers.search },
        { ignoreInputs: false }
      ));
      
      const input = document.createElement('input');
      document.body.appendChild(input);
      
      const event = new KeyboardEvent('keydown', {
        key: 'k',
        bubbles: true,
      });
      Object.defineProperty(event, 'target', { value: input });
      document.dispatchEvent(event);
      
      expect(handlers.search).toHaveBeenCalled();
      
      document.body.removeChild(input);
    });
  });

  describe('enabled option', () => {
    it('should not fire shortcuts when disabled', () => {
      renderHook(() => useKeyboardShortcuts(
        { 'escape': handlers.escape },
        { enabled: false }
      ));
      
      fireKeyDown({ key: 'Escape' });
      
      expect(handlers.escape).not.toHaveBeenCalled();
    });

    it('should fire shortcuts when enabled', () => {
      const { rerender } = renderHook(
        ({ enabled }) => useKeyboardShortcuts(
          { 'escape': handlers.escape },
          { enabled }
        ),
        { initialProps: { enabled: false } }
      );
      
      fireKeyDown({ key: 'Escape' });
      expect(handlers.escape).not.toHaveBeenCalled();
      
      rerender({ enabled: true });
      
      fireKeyDown({ key: 'Escape' });
      expect(handlers.escape).toHaveBeenCalledTimes(1);
    });
  });

  describe('multiple shortcuts', () => {
    it('should handle multiple shortcuts', () => {
      renderHook(() => useKeyboardShortcuts({
        'escape': handlers.escape,
        'ctrl+k': handlers.search,
        '?': handlers.help,
      }));
      
      fireKeyDown({ key: 'Escape' });
      fireKeyDown({ key: 'k', ctrlKey: true });
      fireKeyDown({ key: '/', shiftKey: true });
      
      expect(handlers.escape).toHaveBeenCalledTimes(1);
      expect(handlers.search).toHaveBeenCalledTimes(1);
      expect(handlers.help).toHaveBeenCalledTimes(1);
    });

    it('should only call first matching handler', () => {
      const handler = vi.fn();
      
      // Object key deduplication means only one handler per key
      const shortcuts: Record<string, () => void> = {};
      shortcuts['k'] = vi.fn();
      shortcuts['k'] = handler; // overwrites first
      
      renderHook(() => useKeyboardShortcuts(shortcuts));
      
      fireKeyDown({ key: 'k' });
      
      // Only the last handler should exist due to object key collision
      expect(handler).toHaveBeenCalledTimes(1);
    });
  });

  describe('cleanup', () => {
    it('should remove event listener on unmount', () => {
      const addSpy = vi.spyOn(document, 'addEventListener');
      const removeSpy = vi.spyOn(document, 'removeEventListener');
      
      const { unmount } = renderHook(() => useKeyboardShortcuts({
        'escape': handlers.escape,
      }));
      
      expect(addSpy).toHaveBeenCalledWith('keydown', expect.any(Function));
      
      unmount();
      
      expect(removeSpy).toHaveBeenCalledWith('keydown', expect.any(Function));
      
      addSpy.mockRestore();
      removeSpy.mockRestore();
    });
  });

  describe('return value', () => {
    it('should return showShortcutsModal state', () => {
      const { result } = renderHook(() => useKeyboardShortcuts({}));
      
      expect(result.current.showShortcutsModal).toBe(false);
      
      act(() => {
        result.current.setShowShortcutsModal(true);
      });
      
      expect(result.current.showShortcutsModal).toBe(true);
    });
  });

  describe('SHORTCUTS constant', () => {
    it('should export predefined shortcuts', () => {
      expect(SHORTCUTS.SEARCH).toBe('ctrl+k');
      expect(SHORTCUTS.HELP).toBe('?');
      expect(SHORTCUTS.ESCAPE).toBe('escape');
      expect(SHORTCUTS.REFRESH).toBe('ctrl+r');
      expect(SHORTCUTS.TOGGLE_THEME).toBe('ctrl+shift+t');
    });
  });
});
