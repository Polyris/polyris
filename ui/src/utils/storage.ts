import { logger } from './logger';
// Safe localStorage wrapper with error handling (SSR-safe)
export const safeLocalStorage = {
    getItem(key: string): string | null {
        if (typeof window === 'undefined') return null;
        try {
            return localStorage.getItem(key);
        } catch (e) {
            logger.warn('storage', 'getItem failed', e);
            return null;
        }
    },
    
    setItem(key: string, value: string): void {
        if (typeof window === 'undefined') return;
        try {
            localStorage.setItem(key, value);
        } catch (e) {
            logger.warn('storage', 'setItem failed', e);
        }
    },
    
    removeItem(key: string): void {
        if (typeof window === 'undefined') return;
        try {
            localStorage.removeItem(key);
        } catch (e) {
            logger.warn('storage', 'removeItem failed', e);
        }
    }
};

// Get stored theme preference
export const getStoredTheme = (): string => {
    return safeLocalStorage.getItem('theme') || 'light';
};

// Set theme preference
export const setStoredTheme = (theme: string): void => {
    safeLocalStorage.setItem('theme', theme);
};
