# ADR #66 — Per-Token Scopes (Granular Authorization)

> **Status:** Accepted — implemented in v0.88.0. Follows ADR #65 (PAT + auth
> enforcement). Enforcement ships on (`AUTH_ENABLED=true`, ADR #65); scopes are
> enforced on every non-public request.
> Affects API contract (adds 403) + token shape (`scope` field) → per Core
> Principle #6 this needed alignment before code; this is that alignment.

## Context

After ADR #65 the API authenticates ("who are you?") but does not authorize
("what may this key do?"). Any token = full master-key access: a CI token can
not only read but stop runs, start backfills, **delete assets**, and mint/revoke
other tokens. For a single internal operator this is acceptable (a PAT only
mirrors the operator's own rights — not a hole), but it violates least
privilege and makes a leaked CI token maximally dangerous.

## Decision

Add an ordered scope **level** on each token: **`read` ⊂ `write` ⊂ `admin`**
(each includes those below).

- **read** — all GET (look, don't touch): dashboards, monitoring, CI checks.
- **write** — operational mutations (run, pause, resume, backfill, retry, task
  skip/fail/restart/mark-success, register).
- **admin** — destructive (`asset-delete`, `delete-orphaned`) **and** token
  management (mint/list/revoke). I.e. anything that can destroy data or escalate.

The motivating "CI token may backfill but not delete assets" needs **no**
separate scope: backfill is `write`, deletes are `admin`, so a `write` token
expresses it exactly.

### How the required level is determined

`auth.required_level(method, path)` derives it from the HTTP method —
`GET → read`, `POST/PUT/DELETE → write` — with a tiny `ADMIN_ROUTES` override
set for the five sensitive routes (token CRUD + the two delete routes). This
classifies all 57 routes (31 read / 21 write / 5 admin) with **no** hand-
maintained per-route table, and new routes inherit a sane level from their
method automatically (#1 / #12).

### Enforcement

`auth.authorize(principal, method, path)` runs in the gate **after**
`authenticate()` (so identity is established) and raises `AuthzError → 403` if
the token's scope is below the route's required level. One check, same shape as
the auth gate (#2).

### Principal scope

- **Cognito user (browser operator):** always `admin` — they are the operator.
- **PAT:** its stored `scope`.
- **Legacy PAT minted before scopes (no `scope` field):** treated as `admin` —
  so enabling scopes never breaks an existing token (#4).

### Token shape + creation

`POST /api/tokens` accepts an optional `scope` (`read`|`write`|`admin`),
**defaulting to `read`** (least privilege for new tokens). Stored on the token
record (schemaless DDB — no table change). The UI scope picker (Read-only /
Write / Admin) makes the choice explicit at generation. A too-narrow token
fails **loudly** (clear 403, fixed in seconds); a too-broad token fails
**silently** (the over-permission we're preventing) — hence read-by-default.

### Slack callbacks (`/api/action/*`) — a conscious cut

The four Slack button endpoints (`skip`/`fail`/`success`/`restart`) are plain
**link-buttons** opened from Slack with **no token and no signature**, and were
already open before auth existed. They are added to the gate's public allowlist
(one line) so enabling enforcement doesn't break them. This is a documented,
deliberate cut, **not** an oversight:

- They allow only task-state mutations on a single execution — no data read, no
  deletes, no token access.
- Blast radius is bounded; for a private Slack + single operator the risk is
  negligible. The one real risk is `mark-success` on a failed task (downstream
  could run on bad data) — accepted given the deployment shape.
- **Upgrade path:** signed, expiring action URLs (HMAC of `execution_name` +
  expiry with a server secret), verified in the handler instead of being
  public. Do this when Slack access widens / multiple users / audit matters.

This was also a **rollout prerequisite** independent of scopes: without the
allowlist, flipping `AUTH_ENABLED=true` would 401 every Slack button click.

## Non-goals (v1)

- Per-resource scopes (e.g. `pipelines:read`, `assets:write`) — premature; the
  three coarse levels cover current needs (YAGNI).
- A separate `backfill` scope — expressible as `write` already.
- RBAC / per-user roles beyond the Cognito-operator = admin rule.
- Signed Slack URLs (deferred upgrade path, above).

## Consequences

- A token can now be safely scoped to read-only (dashboards) or write (CI that
  operates but can't destroy), shrinking blast radius on leak.
- Enforcement is additive and backward-compatible: existing tokens (no scope)
  and browser sessions keep working; behavior only changes for new scoped
  tokens, and only when `AUTH_ENABLED=true`.
- `403` is a new response on protected routes; clients should distinguish it
  from `401` (authenticated vs authorized).
- Cost: $0 (a string field on the existing token record).

## CLAUDE.md alignment

- **#1 / #12:** scope derived from method + a 5-entry override; no parallel
  57-row table; `Principal` and the gate are extended, not duplicated.
- **#2:** one `authorize()` check in the handler, mirroring the auth gate.
- **#4 / stability:** legacy + Cognito principals are `admin`; flag-gated; the
  Slack allowlist preserves existing button behavior.
- **#6:** API-contract (403) + token-shape (`scope`) change → this ADR precedes
  code; spike was throwaway and deleted (#11).
- **#13 / #14:** the route→level map is a contract — `test_scopes.py` snapshot-
  tests derivation over the whole route table, and `test_handler_auth_gate.py`
  tests the *enforcement* (403 through `handler`), not just the deriver.
