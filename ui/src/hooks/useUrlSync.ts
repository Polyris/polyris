/**
 * useUrlSync — Synchronize app deep state with URL search params.
 *
 * Generic — accepts any string-keyed object as state. Each route can use
 * this with its own keys (e.g. /pipelines uses {pipeline, mode, date,
 * execution}; /assets uses {asset, tab, group}; /tasks uses {status,
 * date, pipeline, taskName}; /runs uses {status, pipeline}).
 *
 * URL is updated via window.history.pushState/replaceState directly,
 * which means:
 *   - No RSC prefetches triggered on URL change
 *   - No route re-mounts when search params change
 *   - No hydration mismatch risk
 *   - No CloudFront fetch loops
 *
 * Top-level view (/pipelines vs /assets etc) lives in pathname (Next
 * file-system routes), not handled by this hook.
 *
 * Behavior:
 *   - On mount: reads URL params → returns initial state
 *   - updateUrl(state): pushes new URL state (adds history entry)
 *   - replaceUrl(state): replaces URL (no history entry)
 *   - On popstate (back/forward): calls onChange callback
 *   - Values matching `defaults` are omitted from URL (cleaner URLs)
 */

import { useState, useEffect, useCallback, useRef } from 'react';

type UrlStateValue = string | undefined;
type UrlState = Record<string, UrlStateValue>;

interface UseUrlSyncOptions<T extends UrlState> {
    /** Allowed keys for this route. Other URL params are ignored. */
    keys: ReadonlyArray<keyof T & string>;
    /** Default values that should NOT be written to URL (e.g. mode='dag') */
    defaults?: Partial<T>;
    /** Called when user presses back/forward */
    onChange?: (state: Partial<T>) => void;
}

/** Parse current URL search params into state, filtered by allowed keys. */
function parseUrl<T extends UrlState>(keys: ReadonlyArray<keyof T & string>): Partial<T> {
    if (typeof window === 'undefined') return {};
    const params = new URLSearchParams(window.location.search);
    const state: Partial<T> = {};
    for (const k of keys) {
        const v = params.get(k);
        if (v) (state as Record<string, string>)[k] = v;
    }
    return state;
}

/** Build URL from state, preserving current pathname. Defaults are omitted. */
function buildUrl<T extends UrlState>(state: Partial<T>, defaults?: Partial<T>): string {
    if (typeof window === 'undefined') return '';
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(state)) {
        if (v === undefined || v === null || v === '') continue;
        if (defaults && defaults[k as keyof T] === v) continue; // skip defaults
        params.set(k, v as string);
    }
    const search = params.toString();
    return window.location.pathname + (search ? `?${search}` : '');
}

export function useUrlSync<T extends UrlState>(options: UseUrlSyncOptions<T>) {
    const { keys, defaults, onChange } = options;

    const onChangeRef = useRef(onChange);
    useEffect(() => {
        onChangeRef.current = onChange;
    });
    const keysRef = useRef(keys);
    // eslint-disable-next-line react-hooks/refs -- latest-value ref synced for use in effects/callbacks, not read during render
    keysRef.current = keys;
    const defaultsRef = useRef(defaults);
    // eslint-disable-next-line react-hooks/refs -- latest-value ref synced for use in effects/callbacks, not read during render
    defaultsRef.current = defaults;

    // Read initial state from URL on mount (computed once, stable across renders)
    const [initialState] = useState(() => parseUrl<T>(keys));

    // Update URL — push: adds history entry
    const updateUrl = useCallback((state: Partial<T>) => {
        if (typeof window === 'undefined') return;
        const newUrl = buildUrl<T>(state, defaultsRef.current);
        const currentUrl = window.location.pathname + window.location.search;
        if (newUrl !== currentUrl) {
            window.history.pushState(state, '', newUrl);
        }
    }, []);

    // Replace URL (no new history entry — for transient state)
    const replaceUrl = useCallback((state: Partial<T>) => {
        if (typeof window === 'undefined') return;
        const newUrl = buildUrl<T>(state, defaultsRef.current);
        const currentUrl = window.location.pathname + window.location.search;
        if (newUrl !== currentUrl) {
            window.history.replaceState(state, '', newUrl);
        }
    }, []);

    // Listen for back/forward
    useEffect(() => {
        const handlePopState = () => {
            const state = parseUrl<T>(keysRef.current);
            onChangeRef.current?.(state);
        };
        window.addEventListener('popstate', handlePopState);
        return () => window.removeEventListener('popstate', handlePopState);
    }, []);

    return {
        initialState,
        updateUrl,
        replaceUrl,
    };
}

export type { UrlState };
