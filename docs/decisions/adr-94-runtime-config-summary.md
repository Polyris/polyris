---
area: ui
accepted: by 2026-05-31
---
# ADR #94 — Runtime config precedence (window-first)

> Full record: [`adr-94-runtime-config-precedence.md`](./adr-94-runtime-config-precedence.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** `window.CONFIG` (written at load by `/config.js` from CloudFormation
outputs) is authoritative for all UI runtime config; baked `NEXT_PUBLIC_*` is only
a build-time fallback. `getConfig()` in `ui/src/lib/config.ts` is **window-first**,
matching `getApiUrl()` / `isAuthEnabled()`. `AUTH.enabled` uses
`window.CONFIG?.AUTH?.enabled ?? envBool(NEXT_PUBLIC_AUTH_ENABLED)` — `??`, never
`||` or `? :`, so a build-baked `'false'` cannot shadow a runtime `true`.

**Why:** the old env-first form took the truthy string `'false'` baked by
`next.config.mjs` and forced `config.AUTH.enabled=false`; `getAuthHeaders` then
dropped the bearer token while `isAuthEnabled` (window-first) still showed the
app — a blanket `401` once API auth was enabled (CHANGELOG v0.89.5).

**Rule:** never make a config field env-first again; use `??` for booleans so
`false` is not read as "unset". Regression test in `config.test.ts`.
