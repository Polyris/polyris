import { useState, useEffect } from 'react';
import { safeLocalStorage } from '../utils';

/**
 * usePersistedState - Hook for state that persists to localStorage
 */
export function usePersistedState<T>(key: string, defaultValue: T, transform: ((stored: string) => T) | null = null): [T, React.Dispatch<React.SetStateAction<T>>] {
    const [value, setValue] = useState<T>(() => {
        const stored = safeLocalStorage.getItem(key);
        if (stored === null) return defaultValue;
        if (transform) return transform(stored);
        return stored as unknown as T;
    });
    
    useEffect(() => {
        safeLocalStorage.setItem(key, String(value));
    }, [key, value]);
    
    return [value, setValue];
}

/**
 * Predefined transforms for common types
 */
export const transforms = {
    boolean: (val: string) => val !== 'false',
    number: (val: string) => Number(val),
    json: (val: string) => {
        try {
            return JSON.parse(val);
        } catch {
            return null;
        }
    }
};
