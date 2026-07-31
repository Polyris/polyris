'use client';

/**
 * Client-side routing via the History API.
 *
 * The console is a static export served behind a CloudFront SPA rewrite (ADR #41), where
 * Next.js's own router does a full document navigation between the top-level views
 * (/pipelines, /tasks, …) — reloading and re-executing the whole app on every switch. Since
 * every view already renders client-side (behind AuthGate) and CloudFront serves index.html
 * for any path, we drive navigation ourselves with history.pushState. Because Next.js never
 * sees a route change, the mounted page (and <App/>) stays mounted and simply re-renders with
 * the new pathname — so view switches are instant, with no reload or re-fetch.
 *
 * Deep state within /pipelines (?pipeline=&date=&mode=) is handled separately by useUrlSync,
 * which already uses pushState/popstate; this provider only owns the pathname.
 */

import { createContext, useContext, useState, useEffect, useCallback } from 'react';

interface ClientRoute {
    pathname: string;
    push: (url: string) => void;
    replace: (url: string) => void;
}

const RouteContext = createContext<ClientRoute | null>(null);

const pathOf = (url: string): string => url.split('?')[0].split('#')[0];

export function RouteProvider({ children }: { children: React.ReactNode }) {
    const [pathname, setPathname] = useState<string>(() =>
        typeof window !== 'undefined' ? window.location.pathname : '/'
    );

    // Back/forward: reflect the browser's location into React state.
    useEffect(() => {
        const onPopState = () => setPathname(window.location.pathname);
        window.addEventListener('popstate', onPopState);
        return () => window.removeEventListener('popstate', onPopState);
    }, []);

    const push = useCallback((url: string) => {
        if (typeof window === 'undefined') return;
        window.history.pushState(null, '', url);
        setPathname(pathOf(url));
    }, []);

    const replace = useCallback((url: string) => {
        if (typeof window === 'undefined') return;
        window.history.replaceState(null, '', url);
        setPathname(pathOf(url));
    }, []);

    return (
        <RouteContext.Provider value={{ pathname, push, replace }}>
            {children}
        </RouteContext.Provider>
    );
}

export function useClientRoute(): ClientRoute {
    const ctx = useContext(RouteContext);
    if (!ctx) throw new Error('useClientRoute must be used within a RouteProvider');
    return ctx;
}
