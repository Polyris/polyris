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
 * Parse error message from response
 */
const parseError = async (res: Response): Promise<string> => {
    try {
        const body = await res.json();
        return body.error || body.message || `HTTP ${res.status}`;
    } catch {
        return `HTTP ${res.status}`;
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
                const errorMsg = await parseError(res);
                return wrapError(errorMsg, res.status);
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

/**
 * Turn a raw API error string into a short, human-readable message for an error
 * surface (e.g. an EmptyState). Recognizes the common console failure modes —
 * unreachable API, an HTML page where JSON was expected (usually a misconfigured
 * API URL), auth, and 4xx/5xx — and otherwise returns the original text.
 */
export function formatApiErrorMessage(error: string | null | undefined): string {
    if (!error) return 'Something went wrong. Please try again.';
    const e = error.toLowerCase();
    if (e.includes('failed to fetch') || e.includes('networkerror') || e.includes('load failed')) {
        return 'Could not reach the API. Check your connection and that the console API URL is set correctly.';
    }
    if (e.includes('<!doctype') || e.includes('<html') || e.includes('unexpected token')) {
        return 'The API returned an HTML page instead of data — the console API URL is likely misconfigured.';
    }
    if (e.includes('404')) return 'That resource was not found (404) — it may not be available in this edition.';
    if (e.includes('401') || e.includes('403')) return 'You are not authorized to make this request.';
    if (e.includes('500') || e.includes('502') || e.includes('503') || e.includes('504')) {
        return 'The API hit a server error. Please try again in a moment.';
    }
    return error;
}
