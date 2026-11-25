# ADR #65 — API Tokens (PAT) & Auth Enforcement

> Full record: [`adr-65-api-tokens-and-auth-enforcement.md`](./adr-65-api-tokens-and-auth-enforcement.md).
> Summarized here so the ADR index stays the single canonical list.

**Decision:** Add Personal Access Tokens and the missing API auth enforcement in
one move. Enforcement lives in a single gate inside the `console-api` Lambda
(`auth.authenticate`, run before route dispatch under the `/{proxy+}`
integration), **not** an API Gateway authorizer — there was no authorizer and no
token check before this. The gate accepts either a Cognito access token
(browser, verified **offline** via RS256/JWKS and bound to this deployment's app
client) or a PAT (`plrs_…`, SHA-256 hash
lookup). Gated by `AUTH_ENABLED`, **on by default** (the template sets it
`true`; set `false` to disable).

**Key choices:**
- JWT verified **offline** via the pure-Python `rsa` library against the pool
  JWKS (RS256 + issuer + expiry + app-client binding), added through a
  `requirements.txt` in the console_api CodeUri (`sam build` packages it — no
  `--use-container` needed, since `rsa`/`pyasn1` are pure-Python `py3-none-any`
  wheels). No per-request Cognito call, no `cognito-idp` IAM permission. (v0.89.2
  swapped `PyJWT[crypto]`→`rsa` to drop the native `cryptography` wheel, which
  failed to load on Lambda when host-built. An earlier lean used `GetUser`; the offline
  path was implemented instead — see ADR #65, API tokens doc.)
- PATs in a dedicated `api-tokens` table (PAY_PER_REQUEST), not the shared
  `pipeline-tokens` table — keep a credential separate from operational data.
  Only the hash is stored; plaintext shown once. `hash-index` GSI for the auth
  lookup, `owner-index` for listing, TTL on expiry.
- No per-token scopes in v1; `/api/health*` and `/api/metrics` stay public.

**Non-goals (v1):** per-token scopes, RBAC beyond admin/non-admin, offline JWT
verification, a Settings page (tokens live in the `UserMenu` modal).

**Consequences:** turning `AUTH_ENABLED` on breaks any unauthenticated caller
(read-only e2e, UI if it does not send a token everywhere) — mitigated by the
flag + a UI audit + the e2e→PAT migration (rollout phase). `authentication.md`
was corrected (it falsely claimed an API Gateway JWT authorizer existed).
Cost ≈ $0 (on-demand table, no new always-on resource).
