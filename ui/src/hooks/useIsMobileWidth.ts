import { useSyncExternalStore } from 'react';

/** The mobile breakpoint of styles/modules/_mobile.css: below it the sidebar is a drawer and panels are modals. */
export const MOBILE_QUERY = '(max-width: 768px)';

function subscribe(onChange: () => void): () => void {
    if (typeof window.matchMedia !== 'function') return () => {};
    const query = window.matchMedia(MOBILE_QUERY);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
}
const isMobileWidth = () => typeof window.matchMedia === 'function' && window.matchMedia(MOBILE_QUERY).matches;
const isMobileOnServer = () => false;

/** Whether the window is below the mobile breakpoint; follows a resize. */
export function useIsMobileWidth(): boolean {
    return useSyncExternalStore(subscribe, isMobileWidth, isMobileOnServer);
}
