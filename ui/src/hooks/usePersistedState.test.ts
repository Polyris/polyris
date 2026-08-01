import { describe, it, expect, vi, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePersistedState, transforms } from './usePersistedState';

// Mock safeLocalStorage
vi.mock('../utils', () => ({
  safeLocalStorage: {
    getItem: vi.fn(),
    setItem: vi.fn(),
    removeItem: vi.fn(),
  },
}));

import { safeLocalStorage } from '../utils';

describe('usePersistedState', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('initialization', () => {
    it('should return default value when localStorage is empty', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue(null);
      
      const { result } = renderHook(() => 
        usePersistedState('test-key', 'default-value')
      );
      
      expect(result.current[0]).toBe('default-value');
    });

    it('should return stored value when localStorage has value', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue('stored-value');
      
      const { result } = renderHook(() => 
        usePersistedState('test-key', 'default-value')
      );
      
      expect(result.current[0]).toBe('stored-value');
    });

    it('should apply transform function to stored value', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue('42');
      
      const { result } = renderHook(() => 
        usePersistedState('test-key', 0, (val) => parseInt(val, 10))
      );
      
      expect(result.current[0]).toBe(42);
    });
  });

  describe('value updates', () => {
    it('should update state and persist to localStorage', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue(null);
      
      const { result } = renderHook(() => 
        usePersistedState('test-key', 'initial')
      );
      
      act(() => {
        result.current[1]('new-value');
      });
      
      expect(result.current[0]).toBe('new-value');
      expect(safeLocalStorage.setItem).toHaveBeenCalledWith('test-key', 'new-value');
    });

    it('should convert value to string when persisting', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue(null);
      
      renderHook(() => 
        usePersistedState('test-key', true)
      );
      
      expect(safeLocalStorage.setItem).toHaveBeenCalledWith('test-key', 'true');
    });
  });

  describe('transforms', () => {
    it('boolean transform should handle "false" string', () => {
      expect(transforms.boolean('false')).toBe(false);
      expect(transforms.boolean('true')).toBe(true);
      expect(transforms.boolean('anything')).toBe(true);
    });

    it('number transform should parse numeric strings', () => {
      expect(transforms.number('42')).toBe(42);
      expect(transforms.number('3.14')).toBe(3.14);
      expect(transforms.number('invalid')).toBeNaN();
    });

    it('json transform should parse JSON strings', () => {
      expect(transforms.json('{"a":1}')).toEqual({ a: 1 });
      expect(transforms.json('[1,2,3]')).toEqual([1, 2, 3]);
      expect(transforms.json('invalid')).toBeNull();
    });
  });

  describe('edge cases', () => {
    it('should handle empty string stored value', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue('');
      
      const { result } = renderHook(() => 
        usePersistedState('test-key', 'default')
      );
      
      expect(result.current[0]).toBe('');
    });

    it('should persist initial value on mount', () => {
      (safeLocalStorage.getItem as ReturnType<typeof vi.fn>).mockReturnValue(null);
      
      renderHook(() => usePersistedState('test-key', 'initial'));
      
      expect(safeLocalStorage.setItem).toHaveBeenCalledWith('test-key', 'initial');
    });
  });
});
