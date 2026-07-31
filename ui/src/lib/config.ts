/**
 * Application Configuration
 * 
 * Reads from NEXT_PUBLIC_* env vars (build time) or window.CONFIG (runtime, S3/CloudFront).
 * 
 * @module lib/config
 */

declare global {
    interface Window { CONFIG?: {
        API_URL?: string;
        POLL_INTERVAL?: string;
        AUTH?: {
            enabled?: boolean;
            userPoolId?: string;
            clientId?: string;
            region?: string;
        };
    }; }
}

const envBool = (value: string | undefined) => value === 'true' || value === '1';

// Lazy config — reads window.CONFIG at call time, not at module init.
// window.CONFIG is set by /config.js (loaded synchronously before React).
// This ensures API Gateway URL and Cognito settings from CloudFormation
// are always available regardless of module evaluation order.
const getWindowConfig = () => (typeof window !== 'undefined' ? window.CONFIG : undefined);

const getConfig = () => {
    const w = getWindowConfig();
    return {
        API_URL: w?.API_URL || process.env.NEXT_PUBLIC_API_URL || '',
        POLL_INTERVAL: parseInt(
            String(w?.POLL_INTERVAL ?? process.env.NEXT_PUBLIC_POLL_INTERVAL ?? '30000'),
            10
        ),
        AUTH: {
            // window.CONFIG (runtime, written by /config.js from CloudFormation)
            // is authoritative; NEXT_PUBLIC_* is only a build-time fallback.
            // `??` is critical here: next.config.mjs bakes NEXT_PUBLIC_AUTH_ENABLED
            // to the string 'false', and `'false' ? a : b` is TRUTHY — the old
            // env-first form took the env branch and forced enabled=false, ignoring
            // window.CONFIG entirely. getAuthHeaders then dropped the bearer token
            // even though the user was signed in. See CHANGELOG v0.89.5.
            enabled: w?.AUTH?.enabled ?? envBool(process.env.NEXT_PUBLIC_AUTH_ENABLED),
            userPoolId: w?.AUTH?.userPoolId || process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID || '',
            clientId: w?.AUTH?.clientId || process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID || '',
            region: w?.AUTH?.region || process.env.NEXT_PUBLIC_COGNITO_REGION || 'us-east-1',
        },
    };
};

// Default export reads window.CONFIG live on every property access (getters),
// so every consumer — api.ts getAuthHeaders, useAuth, amplifyConfig — sees the
// runtime values whether or not this module evaluated before /config.js ran.
//
// Previously this snapshotted getConfig() ONCE at module load and froze it. When
// the bundle evaluated this module before /config.js had set window.CONFIG,
// AUTH.enabled froze to `false`; getAuthHeaders then silently dropped the
// Authorization header even while the user was fully signed in (useAuth reads the
// flag live and showed the app, so the mismatch stayed invisible until the API
// began enforcing auth). See CHANGELOG v0.89.3.
const config = {
    get API_URL() { return getConfig().API_URL; },
    get POLL_INTERVAL() { return getConfig().POLL_INTERVAL; },
    get AUTH() { return getConfig().AUTH; },
};

export default config;
export const getApiUrl = () => getWindowConfig()?.API_URL || process.env.NEXT_PUBLIC_API_URL || '';
