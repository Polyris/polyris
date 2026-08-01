'use client';

import { useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';

/**
 * Root page — redirects to `/pipelines/` on first mount.
 *
 * IMPORTANT: useEffect deps must be `[]`. Otherwise re-fires on every
 * router/URL change, which combined with previous CloudFront `404 →
 * /index.html` fallback caused infinite redirect loops in production.
 *
 * With CloudFront Function rewriting `/{view}/anything` →
 * `/{view}/index.html` (ADR #41), this page only loads on actual `/`
 * visits or legacy bookmarks like `/?view=assets`.
 */
export default function RootPage() {
    const router = useRouter();
    const ranRef = useRef(false);

    useEffect(() => {
        if (ranRef.current) return;
        ranRef.current = true;

        // Read once from window.location to avoid hook-driven re-renders
        const params = new URLSearchParams(typeof window !== 'undefined' ? window.location.search : '');
        const legacyView = params.get('view');
        // Must match ui/src/types/index.ts MAIN_VIEWS and the CloudFront
        // function regex in sam/template.yaml (ConsoleUiUrlRewriteFunction).
        // If you add a top-level view in App.tsx, update all three.
        const validViews = ['pipelines', 'assets', 'tasks', 'runs', 'backfills'];

        // Preserve legacy deep state params on redirect
        const preserved = new URLSearchParams();
        for (const key of ['pipeline', 'mode', 'date', 'execution']) {
            const v = params.get(key);
            if (v) preserved.set(key, v);
        }
        const search = preserved.toString();

        const target = legacyView && validViews.includes(legacyView)
            ? `/${legacyView}/`
            : '/pipelines/';
        router.replace(search ? `${target}?${search}` : target);
    // Mount-only: must NOT re-fire.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []);

    return null;
}
