/**
 * URL routing helpers shared between App.tsx, Header.tsx, and any other
 * component that needs to derive the top-level view from the current URL.
 *
 * Top-level navigation is path-based (`/pipelines/`, `/assets/`, `/tasks/`,
 * `/runs/`) and served by Next.js file-system routes; CloudFront Function
 * rewrites deep paths to the corresponding `index.html` (ADR #41).
 */

import { isMainView } from '../types';

/**
 * Map URL pathname to top-level view name.
 *
 * Handles trailing slash (`/pipelines/` and `/pipelines` both → `pipelines`),
 * leading slash variations, and unknown paths (defaults to `pipelines`).
 */
export function viewFromPathname(p: string | null): string {
    if (!p) return 'pipelines';
    const segments = p.replace(/^\/+|\/+$/g, '').split('/');
    const first = segments[0] || 'pipelines';
    return isMainView(first) ? first : 'pipelines';
}
