/**
 * api.ts keeps the parsed JSON body of a non-2xx answer as `body` (System status contract):
 * /api/health answers 503 with the checks and findings to show. fetch is the boundary;
 * everything else, config included, runs for real.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { api, setAuthErrorCallback, formatApiErrorMessage } from './api';

const fetchMock = vi.mocked(global.fetch);

/** What fetch resolves to: a JSON body, or (body omitted) one that is not JSON. */
const answer = (status: number, body?: unknown) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
        if (body === undefined) throw new SyntaxError('Unexpected token < in JSON at position 0');
        return body;
    },
}) as unknown as Response;

beforeEach(() => {
    window.CONFIG = { API_URL: 'https://api.test/api' };
});

afterEach(() => {
    delete window.CONFIG;
    setAuthErrorCallback(null);
});

describe('api.get', () => {
    it('keeps the body of a 503: /api/health is an answer even when unhealthy', async () => {
        const body = {
            status: 'unhealthy', detail: 'full',
            checks: { dynamodb: { status: 'unhealthy', message: 'AccessDeniedException' } },
            findings: [{ title: 'The console cannot reach DynamoDB' }],
        };
        fetchMock.mockResolvedValueOnce(answer(503, body));

        const result = await api.get('/health');

        expect(fetchMock).toHaveBeenCalledWith('https://api.test/api/health', expect.objectContaining({ method: 'GET' }));
        expect(result).toEqual({ ok: false, error: 'HTTP 503', status: 503, body });
    });

    it('still takes the message from the body\'s error field', async () => {
        fetchMock.mockResolvedValueOnce(answer(404, { error: 'Pipeline not found' }));
        const result = await api.get('/pipeline-status?name=x');
        expect(result).toEqual({ ok: false, error: 'Pipeline not found', status: 404, body: { error: 'Pipeline not found' } });
    });

    it('adds no body when the error answer is not JSON', async () => {
        fetchMock.mockResolvedValueOnce(answer(502));
        const result = await api.get('/health');
        expect(result).toEqual({ ok: false, error: 'HTTP 502', status: 502 });
        expect(result).not.toHaveProperty('body');
    });

    it('adds no body when the error answer is JSON null, and keeps the HTTP message', async () => {
        fetchMock.mockResolvedValueOnce(answer(500, null));
        const result = await api.get('/health');
        expect(result).toEqual({ ok: false, error: 'HTTP 500', status: 500 });
        expect(result).not.toHaveProperty('body');
    });

    it('answers a 2xx as before: ok, the data, and the data spread', async () => {
        fetchMock.mockResolvedValueOnce(answer(200, { pipelines: [] }));
        expect(await api.get('/pipelines')).toEqual({ ok: true, data: { pipelines: [] }, pipelines: [] });
    });

    it('still reports a 401 to the auth error callback', async () => {
        const onAuthError = vi.fn();
        setAuthErrorCallback(onAuthError);
        fetchMock.mockResolvedValueOnce(answer(401, { message: 'Unauthorized' }));
        expect(await api.get('/pipelines')).toMatchObject({ ok: false, error: 'Unauthorized', status: 401 });
        expect(onAuthError).toHaveBeenCalledTimes(1);
    });

    it('turns a failed fetch into an error result without a body', async () => {
        const errorLog = vi.spyOn(console, 'error').mockImplementation(() => {});
        fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'));
        const result = await api.get('/health');
        expect(result).toEqual({ ok: false, error: 'Failed to fetch' });
        expect(result).not.toHaveProperty('body');
        expect(errorLog).toHaveBeenCalled();
    });
});

describe('formatApiErrorMessage', () => {
    it.each([
        ['Failed to fetch', 'Could not reach the API'],
        ['<!DOCTYPE html><html>', 'returned an HTML page'],
        ['HTTP 401', 'not authorized'],
        ['HTTP 503', 'server error'],
        ['', 'Something went wrong'],
    ])('%j reads as %j', (raw, expected) => {
        expect(formatApiErrorMessage(raw)).toContain(expected);
    });

    it('a 404 says what happened and nothing about other builds (Principle #24)', () => {
        expect(formatApiErrorMessage('HTTP 404')).toBe('That resource was not found (404).');
    });
});
