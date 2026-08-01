# API Tokens (Personal Access Tokens)

> **🔒 Team tier.** Minting and managing PATs — the Console **API Tokens** panel
> and the `POST/GET/DELETE /api/tokens` endpoints — ships in the proprietary
> `ee/` package (ADR #98) and is **not in the open-source build**. The free CE
> gate still *validates* a PAT if one exists, but OSS provides no way to create
> one. **On OSS, authenticate scripts/CI with a Cognito access token**
> (`scripts/get-e2e-token.sh` obtains one) or run with `AUTH_ENABLED=false`. The
> rest of this page applies to Team deployments.

This is the canonical guide for authenticating to the polyris Console API from
outside the browser — scripts, CI, and the API examples in the docs. Browser
sessions use Cognito (see [authentication.md](./authentication.md)); everything
else uses a **Personal Access Token (PAT)**.

Added in v0.87.0 — see ADR #65
(`docs/reference/adr-65-api-tokens-and-auth-enforcement.md`).

> **Enforcement is on by default.** A fresh deployment ships with
> `AUTH_ENABLED=true`, so the API requires a token on every non-public
> request — a Cognito session (browser) or a PAT (scripts/CI). To run without
> auth, set `AUTH_ENABLED=false` and redeploy. See "Enabling enforcement" in
> authentication.md.

## What a PAT is

- A string shaped like `plrs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
- Sent as a normal bearer credential: `Authorization: Bearer plrs_…`.
- Stored server-side **only as a SHA-256 hash** — the plaintext is shown exactly
  once, at creation, and can never be retrieved again.
- Carries a scope — `read`, `write`, or `admin` (see below) — that bounds what
  it can do.
- Optionally time-limited; revocable at any time.

## Scopes (what a token may do)

Each token has a scope — an ordered level, least to most privileged (ADR #66).
Enforced only when `AUTH_ENABLED=true`.

| Scope | Can do | Typical use |
|-------|--------|-------------|
| `read` | All `GET` — view pipelines, runs, assets, metrics | Dashboards, monitoring, CI checks |
| `write` | Read **+** operate: run, pause, backfill, retry, task skip/fail/restart | CI that triggers/operates pipelines |
| `admin` | Write **+** destroy (delete assets) **and** manage tokens (mint/list/revoke) | Your own admin/laptop token |

Higher includes lower (`write` can do everything `read` can). "CI may backfill
but not delete assets" = a `write` token (backfill is write, deletes are admin).

New tokens default to **`read`** (least privilege) — pick a higher scope
explicitly when you need it. A request whose scope is too low gets **403**
(distinct from `401`, which means missing/invalid token). Set the scope in the
Console picker, or via the API:

```bash
curl -X POST "$POLYRIS_API_URL/api/tokens" \
  -H "Authorization: Bearer $POLYRIS_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "ci-pipeline", "scope": "write", "expires_in_days": 90}'
```

Browser (Cognito) sessions always have full access. Tokens created before
scopes existed keep full (`admin`) access.

## Generating a token

### In the Console (recommended)

1. Click your avatar (top-right) → **Settings** → **API Tokens**.
2. **Generate token** → give it a name (e.g. `ci-pipeline`, `laptop`) and an
   optional expiry.
3. Copy the token from the dialog **immediately** — it is shown only once.

### Via the API

```bash
curl -X POST "$POLYRIS_API_URL/api/tokens" \
  -H "Authorization: Bearer $POLYRIS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "ci-pipeline", "expires_in_days": 90}'
# -> 201 { "token_id": "tok_…", "name": "ci-pipeline", "token": "plrs_…", ... }
#    "token" is present ONLY in this response.
```

(Bootstrapping: to create your *first* token you authenticate the call above
with a Cognito token from a browser session, or run it while `AUTH_ENABLED` is
still off.)

## Using a token

Export it once, then every example just works:

```bash
export POLYRIS_API_URL=https://abc123.execute-api.us-east-1.amazonaws.com
export POLYRIS_TOKEN=plrs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

curl -H "Authorization: Bearer $POLYRIS_TOKEN" "$POLYRIS_API_URL/api/pipelines"
```

`/api/health` and `/api/metrics` never require a token.

## Listing and revoking

```bash
# list (hashes/plaintext never returned)
curl -H "Authorization: Bearer $POLYRIS_TOKEN" "$POLYRIS_API_URL/api/tokens"

# revoke by id (from the list, or the Console)
curl -X DELETE -H "Authorization: Bearer $POLYRIS_TOKEN" \
  "$POLYRIS_API_URL/api/tokens?id=tok_ab12cd34"
```

In the Console, the same list lives under avatar → **Settings** → **API Tokens**, each with a
**Revoke** button.

## Security notes

- **Treat a PAT like a password.** It grants your full API access.
- **Store it in a secret manager / CI secret**, never in source control.
- It is shown **once**; if you lose it, revoke it and create a new one.
- Set an **expiry** for CI tokens; expired tokens are rejected and auto-removed
  via DynamoDB TTL.
- Revocation is immediate (subject to a short server-side validation cache).

## CI / e2e

The e2e suite can authenticate with a PAT instead of the Cognito
`admin-initiate-auth` dance — generate one, store it as a CI secret, and pass it
as `POLYRIS_ID_TOKEN` (any valid bearer credential is accepted by the gate).
See `tests/e2e/README.md`.
