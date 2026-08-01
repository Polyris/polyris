/**
 * Structured Logger
 * 
 * Single point for all application logging.
 * - Can be disabled via LOG_ENABLED (e.g., in production)
 * - Consistent format: [LEVEL] context: message
 * - Easy to swap for external service (Sentry, DataDog, etc.)
 */

const LOG_ENABLED = process.env.NODE_ENV !== 'production' || 
    (typeof window !== 'undefined' && new URLSearchParams(window.location?.search).has('debug'));

function formatArgs(context: string, message: string, data?: unknown): unknown[] {
    const args: unknown[] = [`[${context}] ${message}`];
    if (data !== undefined) args.push(data);
    return args;
}

export const logger = {
    /** Warnings — recoverable issues, degraded functionality */
    warn(context: string, message: string, data?: unknown) {
        if (!LOG_ENABLED) return;
        console.warn(...formatArgs(context, message, data));
    },

    /** Errors — failures that need attention */
    error(context: string, message: string, data?: unknown) {
        // Always log errors, even in production
        console.error(...formatArgs(context, message, data));
    },

    /** Debug — verbose info, only in development */
    debug(context: string, message: string, data?: unknown) {
        if (!LOG_ENABLED) return;
        console.log(...formatArgs(context, message, data));
    },
};
