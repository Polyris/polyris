---
area: console-api
accepted: by 2026-05-27
---
# ADR #66 — Per-Token Scopes (Granular Authorization)

> Full record: [`adr-66-per-token-scopes.md`](./adr-66-per-token-scopes.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** Add an ordered scope level on each PAT — **`read` ⊂ `write` ⊂
`admin`** — enforced by `auth.authorize()` in the gate after `authenticate()`
(raises `AuthzError → 403`). Follows ADR #65, API tokens. Takes effect only when
`AUTH_ENABLED=true`.

**Key choices:**
- Required level is **derived from the HTTP method** (`GET`→read, mutations→
  write) plus a 5-entry `ADMIN_ROUTES` override (token CRUD + the two delete
  routes). Classifies all 57 routes (31/21/5) with no hand-maintained table;
  new routes inherit automatically (#1/#12).
- "CI may backfill but not delete" = a `write` token (backfill=write,
  deletes=admin) — no separate `backfill` scope.
- New tokens default to **`read`** (least privilege); the UI picker makes the
  choice explicit. Cognito users = `admin`; **legacy PATs without a scope =
  `admin`** so enabling scopes never breaks an existing token (#4).
- Slack callbacks (`/api/action/*`) added to the public allowlist — a
  documented, conscious cut (link-buttons, no token; only task-state mutations;
  upgrade path = signed expiring URLs). Also a rollout prerequisite: otherwise
  `AUTH_ENABLED=true` would 401 Slack buttons.

**Non-goals (v1):** per-resource scopes, a separate `backfill` scope, RBAC,
signed Slack URLs (deferred).

**Consequences:** tokens can be scoped read-only / write, shrinking leak blast
radius; additive + backward-compatible; `403` is a new response distinct from
`401`. Cost $0 (a field on the token record).
