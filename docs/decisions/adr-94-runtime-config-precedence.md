# ADR #94 — Runtime config precedence (window.CONFIG over baked NEXT_PUBLIC_*)

> **Status:** Accepted — implemented in v0.89.5. Fixes the auth-header bug behind
> the v0.89.x login saga. Pure bug-fix + convention (no API / schema / SFN
> change), documented here so the precedence is not silently reverted.

## Context

The console is a static Next.js export served from S3/CloudFront. Two layers can
supply runtime settings (API URL, Cognito pool, `AUTH.enabled`):

1. **`NEXT_PUBLIC_*` env vars** — inlined into the JS bundle at `next build`.
   `next.config.mjs` gives them defaults, e.g.
   `NEXT_PUBLIC_AUTH_ENABLED: process.env.NEXT_PUBLIC_AUTH_ENABLED || 'false'`,
   so a vanilla build bakes the **string** `'false'` into the bundle.
2. **`window.CONFIG`** — written by `/config.js` at page load from the deployed
   CloudFormation stack outputs (`deploy.sh`). This is the only layer that knows
   the real per-environment values; it is the intended source of truth.

`getConfig()` in `ui/src/lib/config.ts` originally read **env-first**:
`process.env.NEXT_PUBLIC_AUTH_ENABLED ? envBool(env) : window.CONFIG?.AUTH?.enabled`.
Because the baked value `'false'` is a non-empty (truthy) string, the env branch
was always taken and `envBool('false')` → `false`, so `config.AUTH.enabled` was
permanently `false` and `window.CONFIG` was ignored. `useAuth.isAuthEnabled()`
already read `window.CONFIG` first (so the login UI showed and sign-in worked),
but `api.ts` `getAuthHeaders()` read `config.AUTH.enabled` (`false`) and never
attached the bearer token → with `AUTH_ENABLED=true` on the API, every `/api/*`
returned `401 "missing bearer token"` and the app bounced to login. The two
readers disagreeing is what made the bug hard to see.

## Decision

**`window.CONFIG` (runtime) is authoritative for every config field; the baked
`NEXT_PUBLIC_*` values are only a build-time fallback.** `getConfig()` is now
**window-first**, matching `getApiUrl()` and `isAuthEnabled()`:

- Strings (`API_URL`, `userPoolId`, `clientId`, `region`):
  `window.CONFIG.<field> || NEXT_PUBLIC_<field> || <default>`.
- `AUTH.enabled` uses nullish-coalescing, **not** `||` or `? :`:
  `window.CONFIG?.AUTH?.enabled ?? envBool(NEXT_PUBLIC_AUTH_ENABLED)`.
  `??` is required so an explicit runtime `false` is still respected while a
  build-baked `'false'` no longer overrides a runtime `true`.

`config` stays a lazy getter object (see CHANGELOG v0.89.3) so the window-first
read happens at access time — `/config.js` may run after this module is evaluated.

## Rule for future changes

- **Do not make any config field env-first again.** A truthy build-time default
  (like `'false'`) will shadow the runtime value and silently break consumers.
- New runtime settings follow the same shape: `window.CONFIG` first,
  `NEXT_PUBLIC_*` as fallback, and `??` (never `||` / `? :`) for any **boolean**
  so `false` is not treated as "unset".
- A regression test in `ui/src/lib/config.test.ts` pins that
  `window.CONFIG.AUTH.enabled` wins over a stubbed `NEXT_PUBLIC_AUTH_ENABLED='false'`.
  Vitest does not apply `next.config.mjs` env-baking, so the env must be stubbed
  explicitly to reproduce the bug — a plain "set window.CONFIG and read" test
  passes even against the buggy env-first code and is **not** sufficient.

## Consequences

- One source of truth for runtime config at the read sites (`getAuthHeaders`,
  `useAuth`, `amplifyConfig`) — no more reader disagreement.
- `next.config.mjs` keeping `|| 'false'` is now harmless (fallback only); left
  as-is to avoid churn.
- No API / schema / SFN change; UI-only.
