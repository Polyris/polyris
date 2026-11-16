import { useState, useEffect } from 'react';
import { safeLocalStorage } from '../utils';

/**
 * usePersistedState - Hook for state that persists to localStorage
 *
 * `transform` reads the stored string back; `serialize` writes the value
 * (default `String`, so an object needs e.g. `JSON.stringify`). Pass a stable
 * function: an inline arrow is a new effect dependency every render, and
 * rewrites localStorage each time.
 */
export function usePersistedState<T>(
    key: string,
    defaultValue: T,
    transform: ((stored: string) => T) | null = null,
    serialize: (value: T) => string = String,
): [T, React.Dispatch<React.SetStateAction<T>>] {
    const [value, setValue] = useState<T>(() => {
        const stored = safeLocalStorage.getItem(key);
        if (stored === null) return defaultValue;
        if (transform) return transform(stored);
        return stored as unknown as T;
    });

    useEffect(() => {
        safeLocalStorage.setItem(key, serialize(value));
    }, [key, value, serialize]);
    
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
