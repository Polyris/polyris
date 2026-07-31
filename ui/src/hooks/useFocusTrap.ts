import { useEffect, useRef, useCallback } from 'react';

/**
 * useFocusTrap — Trap keyboard focus within a container element.
 * 
 * Used for custom dialogs/panels not built on BaseModal.
 * BaseModal already has built-in focus trap — use this only for
 * dropdowns, popovers, or custom overlay components.
 * 
 * @param isActive - Whether the focus trap is currently active
 * @returns ref to attach to the container element
 */
export function useFocusTrap<T extends HTMLElement = HTMLDivElement>(isActive: boolean) {
    const containerRef = useRef<T>(null);
    const previouslyFocused = useRef<HTMLElement | null>(null);

    const getFocusableElements = useCallback((): HTMLElement[] => {
        if (!containerRef.current) return [];
        
        const selectors = [
            'a[href]',
            'button:not([disabled])',
            'input:not([disabled])',
            'select:not([disabled])',
            'textarea:not([disabled])',
            '[tabindex]:not([tabindex="-1"])',
        ].join(', ');
        
        return Array.from(
            containerRef.current.querySelectorAll<HTMLElement>(selectors)
        ).filter(el => el.offsetParent !== null);
    }, []);

    useEffect(() => {
        if (!isActive) return;

        previouslyFocused.current = document.activeElement as HTMLElement;

        const timer = setTimeout(() => {
            const focusable = getFocusableElements();
            if (focusable.length > 0) {
                focusable[0].focus();
            } else if (containerRef.current) {
                containerRef.current.focus();
            }
        }, 50);

        const handleKeyDown = (e: KeyboardEvent) => {
            if (e.key !== 'Tab') return;

            const focusable = getFocusableElements();
            if (focusable.length === 0) return;

            const first = focusable[0];
            const last = focusable[focusable.length - 1];

            if (e.shiftKey) {
                if (document.activeElement === first) {
                    e.preventDefault();
                    last.focus();
                }
            } else {
                if (document.activeElement === last) {
                    e.preventDefault();
                    first.focus();
                }
            }
        };

        document.addEventListener('keydown', handleKeyDown);

        return () => {
            clearTimeout(timer);
            document.removeEventListener('keydown', handleKeyDown);
            if (previouslyFocused.current?.focus) {
                previouslyFocused.current.focus();
            }
        };
    }, [isActive, getFocusableElements]);

    return containerRef;
}
