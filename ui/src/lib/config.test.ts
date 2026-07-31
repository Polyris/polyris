/**
 * Regression guard for the v0.89.3 bug: the default `config` export must read
 * window.CONFIG LAZILY (getters), not snapshot+freeze it at module load.
 *
 * Why it matters: /config.js (which sets window.CONFIG at runtime) may run after
 * this module is evaluated. If config froze a snapshot taken before that, AUTH
 * .enabled became `false` and api.ts/getAuthHeaders silently dropped the bearer
 * token while the user was signed in. These tests fail if config goes eager again.
 *
 * NOTE: deliberately does NOT vi.mock('./config') — it exercises the real module.
 */
import { describe, it, expect, afterEach, vi } from 'vitest';
import config from './config';

declare global {
    interface Window { CONFIG?: Record<string, unknown>; }
}

describe('config — lazy window.CONFIG reads', () => {
    const original = window.CONFIG;
    afterEach(() => { window.CONFIG = original; vi.unstubAllEnvs(); });

    it('reflects window.CONFIG.AUTH.enabled set AFTER module import (not a frozen snapshot)', () => {
        window.CONFIG = { AUTH: { enabled: true, userPoolId: 'p', clientId: 'c', region: 'us-east-1' } };
        expect(config.AUTH.enabled).toBe(true);
    });

    it('re-reads on every access, so a later change is observed (proves it is not frozen)', () => {
        window.CONFIG = { AUTH: { enabled: true } };
        expect(config.AUTH.enabled).toBe(true);
        window.CONFIG = { AUTH: { enabled: false } };
        expect(config.AUTH.enabled).toBe(false);
    });

    it('reads API_URL live from window.CONFIG', () => {
        window.CONFIG = { API_URL: 'https://example.test/api' };
        expect(config.API_URL).toBe('https://example.test/api');
    });

    it('defaults AUTH.enabled to false when window.CONFIG is absent', () => {
        window.CONFIG = undefined;
        expect(config.AUTH.enabled).toBe(false);
    });

    it('window.CONFIG.AUTH.enabled wins over a build-baked NEXT_PUBLIC_AUTH_ENABLED=false (v0.89.5 bug)', () => {
        // next.config.mjs bakes NEXT_PUBLIC_AUTH_ENABLED='false'; the runtime
        // window.CONFIG must still win, or getAuthHeaders drops the bearer token.
        vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', 'false');
        window.CONFIG = { AUTH: { enabled: true } };
        expect(config.AUTH.enabled).toBe(true);
    });

    it('falls back to NEXT_PUBLIC_AUTH_ENABLED only when window.CONFIG.AUTH is absent', () => {
        vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', 'true');
        window.CONFIG = {};
        expect(config.AUTH.enabled).toBe(true);
    });

    it('treats AUTH.enabled: null (local-dev sentinel) as fall-through to .env, not false', () => {
        // ui/public/config.js ships enabled: null so local .env drives auth. The
        // resolver uses `?? env`, so null must fall through — a `!== undefined`
        // check (which useAuth once had) would wrongly read null as "not true" and
        // silently disable auth even with NEXT_PUBLIC_AUTH_ENABLED=true.
        vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', 'true');
        window.CONFIG = { AUTH: { enabled: null as unknown as boolean, userPoolId: '', clientId: '', region: 'us-east-1' } };
        expect(config.AUTH.enabled).toBe(true);

        vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', 'false');
        expect(config.AUTH.enabled).toBe(false);
    });
});
