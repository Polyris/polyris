# ADR #65 — API Tokens (PAT) & Auth Enforcement

> **Status:** Accepted — implemented in v0.87.0 (backend, UI, docs). Enforcement
> ships **on** (`AUTH_ENABLED=true`): the UI attaches a token on every request
> and e2e uses a PAT, so the Principle #4 preconditions are met and the gate is
> enabled from the initial deploy. Disabling is operator-side
> (`AUTH_ENABLED=false`).
> **Indexed** as ADR #65 (summary in `DESIGN_DECISIONS.md`; note its headers
> top out at #64 while `CLAUDE.md` references higher numbers — reconcile the
> numbering at your convenience).
> Affected DDB schema + API contract + auth → per Core Principle #6 this needed
> alignment before code; this document was that alignment + the build plan.
>
> **Implementation note (supersedes Q2 below):** the JWT half was implemented
> with **offline JWKS/RS256 verification** (added through a
> `console_api/requirements.txt`, packaged by `sam build`), with app-client
> binding and no per-request Cognito call. Q2 records the original `GetUser`
> lean; the offline path was built instead, which is the stronger design.
> Verification uses the pure-Python `rsa` library (v0.89.2 swapped the original
> `PyJWT[crypto]`, whose native `cryptography` wheel failed to load on Lambda
> when built on the host — `sam build` now needs no `--use-container`; see CHANGELOG).

---

## Context

Two user-facing pains started this:

1. The API usage examples (`docs/operations/API.md`, README, etc.) are hard to
   actually run — they require a Cognito JWT obtained from a **browser** login.
2. E2E tests need a hack (`admin-initiate-auth` via AWS CLI) for the same reason.

Investigating the root cause surfaced something bigger: **there is no auth
enforcement in the codebase today.**

- No authorizer is attached to the `ConsoleApi` HttpApi in `template.yaml`
  (the only `AuthorizationType: API_KEY` is on the Slack EventBridge connection).
- `console_api/main.py` `handler()` validates **nothing** — it goes CORS →
  route lookup → handler. No `Authorization` parsing, no token check.
- No post-deploy script attaches an authorizer.
- `docs/features/authentication.md` **claims** "API Gateway validates tokens
  before forwarding to Lambda" and draws a "JWT Authorizer" box. Both are false.
  Per Principle #9, that doc is a live bug today, independent of this feature.

Cognito resources (user pool + client) exist and the UI logs in via Amplify, but
the token it sends is never checked. The API is effectively open.

## Decision

Add **Personal Access Tokens (PAT)** and, in the same work, build the missing
enforcement **once, covering both auth types** through a single gate.

- **One auth gate** at the top of `console_api/main.py` `handler()` — same "one
  helper, all 57 routes" shape as `cors_response`. Branch on token shape:
  `plrs_<...>` → PAT verification; otherwise → Cognito access-token verification.
- **PAT mechanics** (proven by `spike/pat_auth_spike.py`): `plrs_` +
  `secrets.token_urlsafe(32)`; store **only** the SHA-256 hash; constant-time
  compare (`hmac.compare_digest`); support revoke + expiry; plaintext shown
  **once** at creation, never again.
- **Editions:** Cognito/login exists in **both** editions. Pro is **not**
  Cognito — Pro is the *in-app* user-management screen (`Manage Users`). In open
  core, users are created by an admin via the AWS console
  (`admin-create-user` + `admin-set-user-password --permanent` — the same flow
  the e2e helper already uses). The dual-auth gate (JWT + PAT) is active in
  **both** editions; there is no edition-dependent auth logic.
- **UI:** the existing `UserMenu` dropdown gets a new **API Tokens** item
  (personal, visible to all authenticated users — separate from the admin-only
  `Manage Users`). It opens a modal (reusing `BaseModal`) with the token list
  (name / created / last used / Revoke) plus Generate → "shown once".

## Resolved questions (leans applied; flag before build if you disagree)

1. **Is enforcement really absent?** Code says yes. **Decision:** treat the gate
   as the single source of enforcement, behind an `AUTH_ENABLED` flag.
   Phase 0 (`make e2e-health`) confirms at runtime; if it returns 401 there is an
   out-of-band authorizer configured by hand → remove it and let the gate own
   auth (no config drift outside IaC).

2. **JWT verification method.** `console_api` has **no dependency-packaging
   mechanism** (no `requirements.txt`, no Lambda layer — only runtime boto3 + the
   symlinked `polyris`). Hand-rolling RS256 is forbidden by #12. **Decision (v1):
   verify the Cognito *access* token via boto3 `cognito-idp:GetUser`** — zero new
   dependencies, no crypto, no JWKS plumbing. Cost: one Cognito call per JWT
   request; cache the result with a short TTL to keep it cheap (low volume makes
   this negligible). **Upgrade path** (offline verification, no per-request
   Cognito call): add `PyJWT[crypto]` + JWKS — but that requires introducing a
   Lambda layer or `requirements.txt` + `sam build`. Deferred until the
   per-request call is shown to matter.
   - Note: `GetUser` validates the **access** token. Confirm the UI attaches the
     access token (Amplify exposes both id + access) — see Phase 4.

3. **Token storage.** **Decision:** a **new** `api-tokens` table
   (`PAY_PER_REQUEST`, zero idle cost — confirmed all 7 tables are on-demand).
   Keep security credentials separate from execution data (do **not** reuse
   `pipeline-tokens` with a discriminator, despite the ADR #51 precedent).
   Schema: `PK = token_id` (public, e.g. `tok_ab12cd34`); GSI `hash-index`
   (`PK = token_hash`) for the hot auth lookup; GSI `owner-index`
   (`PK = owner_sub`) for "list my tokens" (Pro multi-user); TTL on `expires_at`.

4. **v1 scope.** **Decision:** no per-token scopes — a token grants the full API
   access of its owner. `/api/health*` stays public (allowlist in the gate).

5. **Rollout.** **Decision:** ship the gate + PAT with `AUTH_ENABLED=true`
   (enforcement on by default). Principle #4 is satisfied because the UI attaches
   a token on every request and e2e uses a PAT **before** this ships; the flag
   stays the lever to disable enforcement if ever needed.

## Non-goals (v1)

- Per-token scopes / fine-grained permissions.
- RBAC beyond the existing admin/non-admin split.
- Offline JWT verification (PyJWT/JWKS) — deferred (see Q2).
- A separate Settings page — tokens live in the `UserMenu` modal.
- An admin "create token for another user" path.

## Consequences

- **Principle #4 was the main risk.** Enforcement on rejects any unauthenticated
  caller, so the UI must attach a token to **every** request and e2e must use a
  PAT. Both were completed (UI token attachment + e2e PAT migration + the
  Phase 4 audit) **before** enabling the flag, so shipping on is safe.
  `AUTH_ENABLED=false` remains the lever to disable enforcement.
- **Principle #9:** `authentication.md` must be corrected (it is wrong today).
- **Positive:** API examples become copy-paste (`Authorization: Bearer plrs_…`);
  e2e drops the `admin-initiate-auth` hack in favour of a PAT; enforcement
  finally lives in IaC + code instead of nowhere.
- **Cost:** ≈ $0 to the ~$51/mo baseline. No new always-on resource; the
  `api-tokens` table is on-demand. Per-request: PAT path = 1 DDB read; JWT path =
  1 cached Cognito call. Levers: cache JWKS/GetUser; do **not** write
  `last_used_at` synchronously on every call (throttle/coarsen — writes cost ~5×
  reads).

## CLAUDE.md alignment

- **#6** — DDB schema + API contract + auth: this ADR is the required alignment;
  no code lands until it is approved.
- **#1 / #2 / #12** — one gate (like `cors_response`); `api_tokens_repo` mirrors
  `backfills_repo`; reuse `BaseModal` / `ConfirmModal`; stdlib crypto only; JWT
  via the AWS platform (`GetUser`) rather than hand-rolled or a heavy dep.
- **#3** — token create/revoke use conditional writes (idempotent).
- **#9 / #10** — docs corrected + added in the same delivery, English-only.
- **#11** — no fake auth path; when disabled (`AUTH_ENABLED=false`) the gate is
  an honest no-op, not a stub or a fake-allow.
- **#13 / #14** — tests mock only the boundary: `moto` for DDB, a stubbed
  `cognito-idp` client for JWT, **real** hashing for PAT.
- **API Routes checklist** — each new route goes through all 5 steps incl. the
  route-count assert in `tests/sdk/test_templates.py`.

---

## Implementation plan

### Phase 0 — confirm reality (you, 1 command)
- `export POLYRIS_API_URL=… && make e2e-health`
  - **200** → enforcement absent (expected). Proceed.
  - **401/403** → an out-of-band authorizer exists; add a Phase-1 step to remove
    it and move ownership to the gate.

### Phase 1 — token model, storage, gate (no enforcement yet)
- `template.yaml`: new `ApiTokensTable` (`PAY_PER_REQUEST`, `PK=token_id`,
  GSI `hash-index`, GSI `owner-index`, TTL `expires_at`); add `API_TOKENS_TABLE`
  env var to `console-api`.
- `console_api/dal/api_tokens_repo.py`: repo mirroring `backfills_repo`
  (`get_by_id`, `get_by_hash`, `list_by_owner`, `put`, `revoke`). Conditional
  writes (#3).
- `console_api/auth.py` (new): `generate_pat()`, `_hash()`, `verify_pat()`,
  `verify_cognito_token()` (boto3 `GetUser`, cached), `authenticate(event)`
  (the gate, with health allowlist), `AuthError`. Lift the proven logic from
  `spike/pat_auth_spike.py`; delete the spike afterward (#11 — no spike code in
  prod).
- Tests: `tests/.../dal/test_api_tokens_repo.py` (moto), `test_auth.py`
  (real hashing; stubbed `cognito-idp`). #13/#14.

### Phase 2 — CRUD endpoints (5-step route process ×3)
- `routes/tokens.py`: `create_token` (`POST /api/tokens`) → returns plaintext
  **once**; `list_tokens` (`GET /api/tokens`); `revoke_token`
  (`DELETE /api/tokens/{id}`). `cors_response`, `log.error`, error visibility
  (ADR #38).
- `routes/__init__.py` exports → `main.py` imports + `ROUTES` entries →
  `template.yaml` routes → bump route-count assert in
  `tests/sdk/test_templates.py` (52 → 55).

### Phase 3 — wire the gate (behind the flag)
- `main.py` `handler()`: after the OPTIONS short-circuit, before dispatch:
  ```python
  if AUTH_ENABLED and path not in PUBLIC_PATHS:
      try:
          event['principal'] = authenticate(event, api_tokens_repo)
      except AuthError as e:
          return error_response(401, 'UNAUTHORIZED', str(e), request_id=request_id)
  ```
- `AUTH_ENABLED` defaults **false** → zero behavior change (#4).

### Phase 4 — UI
- `UserMenu.tsx`: add **API Tokens** item (all users; above/around the admin
  `Manage Users`), firing an `onManageTokens` callback (mirror `onManageUsers`).
- `ApiTokensModal.tsx` (new, reuse `BaseModal`): list + Revoke (via
  `ConfirmModal`) + Generate → "shown once" with copy.
- React Query hooks for the 3 endpoints; Vitest tests.
- **Audit (critical for #4):** confirm the fetch/`useAuth` layer attaches the
  Cognito **access** token on **every** API request (matches Q2's `GetUser`).

### Phase 5 — docs (A–E)
- **Fix** `docs/features/authentication.md` — remove the fictional authorizer,
  document the real gate, dual auth, and `AUTH_ENABLED`; correct the diagram.
- **New** `docs/features/api-tokens.md` — generate (UI), use (curl/CLI), expiry,
  revoke, security (shown once, store as secret). This is the **single** canonical
  auth-how-to.
- **Update examples** to add `-H "Authorization: Bearer $POLYRIS_TOKEN"`:
  `docs/operations/API.md` (+ a short auth preamble), `README.md`,
  `docs/getting-started/SETUP_FROM_SCRATCH.md` (first-token bootstrap),
  `docs/operations/TROUBLESHOOTING.md` (401s), `docs/tools/AI_ASSISTANT.md`, and
  the `questions` surface (confirm location). Per #1: link to api-tokens.md, do
  not copy the preamble into each file.
- **E2E docs:** `tests/e2e/README.md` + `scripts/get-e2e-token.sh` story — add
  PAT as the recommended path (keep JWT as an option).
- Merge this ADR into `DESIGN_DECISIONS.md`; add a CHANGELOG entry; bump version
  in `pyproject.toml`, `polyris/__init__.py`, `ui/package.json` (kept in sync).

### Phase 6 — rollout (done in v0.89.x)
- `AUTH_ENABLED=true` is the template default (enforcement on from initial deploy).
- e2e migrated to a PAT; `make e2e-smoke` → green.
- UI verified end-to-end with enforcement on.

### Verification gate (before delivery — CLAUDE.md Testing)
`make lint` · `make sync-constants` · `make check-versions` ·
`make smoke-pipelines` · full suite across **all** `pytest.ini`
(`find . -name pytest.ini`) · UI tests · `cfn-lint` · route-count assert.

### Suggested versioning
Backend-and-UI feature touching schema + API + auth → minor bump (e.g. v0.87.0).
Optionally split: v0.87.0 backend (Phases 1–3) → v0.87.1 UI + docs (Phases 4–6).

## Update — v0.89.10: the token list shows active tokens only

Revocation is a soft-delete (`revoked` + `revoked_at`) so a reused token is
rejected precisely ("token revoked") and the record survives for a future audit
log. `GET /api/tokens` filters revoked tokens out of the response — the list is
for *usable* credentials; revocation **history** belongs in the audit log (a
planned Pro feature), not in the active list. A `?include_revoked` view can be
added with that feature if ever needed.
