import { logger } from './logger';
import { API } from './constants';
import { getApiUrl as getConfigApiUrl } from '../lib/config';
import config from '../lib/config';
import type { ApiResult } from '../types';

// API URL from config — lazy read from window.CONFIG every call
const getApiUrl = () => getConfigApiUrl();

/**
 * Wraps API response in Result type for cleaner error handling
 */
const wrapResult = <T = unknown>(data: T) => ({ ok: true as const, data });
const wrapError = (message: string, status?: number) => ({ ok: false as const, error: message, status });

/**
 * Parse a non-2xx response: its error message and, when it is JSON, its body.
 *
 * The body is kept because some error answers carry data: /api/health answers 503
 * with the checks and findings the System status shows. A caller that reads only
 * `error` sees the same message as before.
 */
const parseError = async (res: Response): Promise<{ message: string; body?: unknown }> => {
    try {
        const body = await res.json();
        return { message: body.error || body.message || `HTTP ${res.status}`, body };
    } catch {
        return { message: `HTTP ${res.status}` };
    }
};

// -----------------------------------------------------------------------------
// Auth Token Management
// -----------------------------------------------------------------------------

type AuthTokenGetter = () => Promise<string | null>;
type AuthErrorCallback = () => void;

let _getAuthToken: AuthTokenGetter | null = null;
let _onAuthError: AuthErrorCallback | null = null;

export const setAuthTokenGetter = (getter: AuthTokenGetter | null) => {
    _getAuthToken = getter;
};

export const setAuthErrorCallback = (callback: AuthErrorCallback | null) => {
    _onAuthError = callback;
};

const getAuthHeaders = async (): Promise<Record<string, string>> => {
    if (!config.AUTH?.enabled) {
        return {};
    }
    if (_getAuthToken) {
        try {
            const token = await _getAuthToken();
            if (token) {
                return { Authorization: `Bearer ${token}` };
            }
        } catch (e) {
            logger.warn('api', 'Failed to get auth token', e);
        }
    }
    return {};
};

const handleAuthError = (status: number) => {
    if (status === 401 && _onAuthError) {
        _onAuthError();
    }
};

// -----------------------------------------------------------------------------
// Base request method
// -----------------------------------------------------------------------------

interface RequestOptions {
    body?: unknown;
    timeout?: number;
}

const _request = async (method: string, path: string, options: RequestOptions = {}) => {
    const { body = null, timeout = API.TIMEOUT } = options;

    const doFetch = async () => {
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), timeout);

        try {
            const authHeaders = await getAuthHeaders();
            const headers: Record<string, string> = { ...authHeaders };
            
            if (body !== null) {
                headers['Content-Type'] = 'application/json';
            }

            const fetchOptions: RequestInit = {
                method,
                headers,
                signal: controller.signal,
            };
            if (body !== null) {
                fetchOptions.body = JSON.stringify(body);
            }

            const res = await fetch(`${getApiUrl()}${path}`, fetchOptions);
            clearTimeout(timeoutId);

            if (!res.ok) {
                handleAuthError(res.status);
                const { message, body: errorBody } = await parseError(res);
                const failed = wrapError(message, res.status);
                return errorBody === undefined ? failed : { ...failed, body: errorBody };
            }

            const data = await res.json();
            return { ...wrapResult(data), ...data };
        } catch (e: unknown) {
            clearTimeout(timeoutId);
            if (e instanceof Error && e.name === 'AbortError') {
                return wrapError('Request timeout');
            }
            throw e;
        }
    };

    try {
        return await doFetch();
    } catch (e: unknown) {
        logger.error('api', `${method} ${path} failed`, e);
        return { ok: false as const, error: e instanceof Error ? e.message : String(e) };
    }
};

// -----------------------------------------------------------------------------
// Public API
// -----------------------------------------------------------------------------

export const api = {
    get(path: string, options: RequestOptions = {}) {
        return _request('GET', path, options);
    },
    post(path: string, data?: unknown, options: RequestOptions = {}) {
        return _request('POST', path, { ...options, body: data });
    },
    put(path: string, data?: unknown, options: RequestOptions = {}) {
        return _request('PUT', path, { ...options, body: data });
    },
    delete(path: string, options: RequestOptions = {}) {
        return _request('DELETE', path, options);
    },
};

export const isOk = <T = unknown>(result: ApiResult<T>): result is { ok: true; data: T } => result.ok === true;

/** A failed request thrown from a query, carrying the HTTP status when the API answered. */
export class ApiError extends Error {
    constructor(message: string, readonly status?: number) {
        super(message);
        this.name = 'ApiError';
    }
}

/** What kind of failure an API error is. `unreachable`, `timeout` and `misconfigured` mean the
 *  console has no API to talk to; the others are answers from it. */
export type ApiErrorKind = 'unreachable' | 'timeout' | 'misconfigured' | 'not_found' | 'unauthorized' | 'server' | 'other';

/**
 * Classifies by the HTTP status when there is one: the request's, else the one its own
 * `HTTP 404` wording names. A number inside an id (`run-…-a404b2c1`) is never a status.
 * Without a status the request got no answer: the network, a timeout, or a page that is not JSON.
 */
export function classifyApiError(error: string | null | undefined, status?: number): ApiErrorKind {
    const e = (error ?? '').toLowerCase();
    const named = /\bhttp (\d{3})\b/.exec(e);
    const code = status ?? (named ? Number(named[1]) : undefined);
    if (code === undefined) {
        if (e.includes('failed to fetch') || e.includes('networkerror') || e.includes('load failed')) return 'unreachable';
        if (e.includes('request timeout')) return 'timeout';
        if (e.includes('<!doctype') || e.includes('<html') || e.includes('unexpected token')) return 'misconfigured';
        return 'other';
    }
    if (code === 404) return 'not_found';
    if (code === 401 || code === 403) return 'unauthorized';
    if (code >= 500) return 'server';
    return 'other';
}

/**
 * Turn a raw API error string into a short, human-readable message for an error
 * surface (e.g. an EmptyState). Recognizes the common console failure modes —
 * unreachable API, an HTML page where JSON was expected (usually a misconfigured
 * API URL), auth, and 4xx/5xx — and otherwise returns the original text.
 */
export function formatApiErrorMessage(error: string | null | undefined, status?: number): string {
    if (!error) return 'Something went wrong. Please try again.';
    switch (classifyApiError(error, status)) {
        case 'unreachable':
            return 'Could not reach the API. Check your connection and that the console API URL is set correctly.';
        case 'timeout':
            return 'The API did not answer in time. Check your connection and try again.';
        case 'misconfigured':
            return 'The API returned an HTML page instead of data — the console API URL is likely misconfigured.';
        case 'not_found':
            return 'That resource was not found (404).';
        case 'unauthorized':
            return 'You are not authorized to make this request.';
        case 'server':
            return 'The API hit a server error. Please try again in a moment.';
        default:
            return error;
    }
}

/** A failed read of the pipelines list, as the header and the sidebar show it. */
export interface ConnectionProblem {
    /** The console has no API to talk to (as opposed to an API that answered with an error). */
    unreachable: boolean;
    /** formatApiErrorMessage's words. */
    message: string;
}

/** A query's error (an ApiError, any Error, or a message) as the header and the sidebar show it. */
export function connectionProblem(error: unknown): ConnectionProblem {
    const message = error instanceof Error ? error.message : String(error ?? '');
    const status = error instanceof ApiError ? error.status : undefined;
    const kind = classifyApiError(message, status);
    return {
        unreachable: kind === 'unreachable' || kind === 'timeout' || kind === 'misconfigured',
        message: formatApiErrorMessage(message, status),
    };
}
