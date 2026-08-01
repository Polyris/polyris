"""
Auth gate for Console API (ADR #65).

Single entry-point authentication for the proxy Lambda. The API is fronted by
a `/{proxy+}` HTTP API integration, so every request flows through main.handler;
this module provides the one check that runs there (the same "one helper, all
routes" shape as cors_response).

Accepts either credential on `Authorization: Bearer <token>`:
  - polyris Personal Access Token (PAT), "plrs_..."  -> SHA-256 hash lookup
  - Cognito access/ID token (browser via Amplify)     -> offline JWKS RS256 verify

Enforcement is gated by AUTH_ENABLED and is on by default (the deployed
template sets it "true"); disabling it is a deliberate, reversible step
(Core Principle #4). Health/metrics paths are always public (probes, load
balancers).

JWT note: the Cognito token is verified **offline** against the pool's JWKS
(RS256 signature + issuer + expiry), and the token's client id is checked
against this deployment's app client (`client_id` for access tokens, `aud` for
ID tokens). No per-request Cognito API call; signing keys are fetched once and
cached. Verification is pure-Python (`rsa` for the RSASSA-PKCS1-v1_5/SHA-256
check) — no native `cryptography` wheel, so `sam build` needs no `--use-container`.
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from typing import Optional, Tuple

import rsa

from logger import log

# Token format: prefix keeps the gate's branch trivial and makes a leaked
# token greppable in logs / secret scanners (cf. GitHub's `ghp_`).
TOKEN_PREFIX = "plrs_"
TOKEN_ENTROPY_BYTES = 32  # 256 bits

# Always-public path prefixes (no token required).
#   /api/health*, /api/metrics — probes / load balancers.
#   /api/action/*             — Slack button callbacks (ADR #66): these are
#     plain link-buttons opened from Slack with no token and no signature, and
#     were already open before auth existed. Left public as a documented,
#     conscious cut (the only mutations they allow are task skip/retry/fail/
#     mark-success on a single execution; no data read, no deletes). Upgrade
#     path: signed expiring URLs.
PUBLIC_PATH_PREFIXES = ("/api/health", "/api/metrics", "/api/action")

# --- scope model (ADR #66) -------------------------------------------------
# Ordered level: read ⊂ write ⊂ admin. Each level includes the ones below.
#   read  : all GET (look, don't touch)
#   write : operational mutations (run, pause, backfill, retry, task ops)
#   admin : destructive (asset delete) + token management (mint/list/revoke keys)
# "CI token may backfill but not delete" == a write token (backfill=write,
# deletes=admin) — no separate scope needed.
#
# SSoT note (ADR #66): this vocabulary has no codegen (codegen covers only the
# pipeline schema, not console_api constants). If you change the scope set,
# also update the UI picker (ui/src/components/ApiTokensSection.tsx <option>s)
# and docs/features/api-tokens.md. Three stable values — a deliberate accepted
# trade-off, not codegen-worthy.
SCOPE_LEVELS = {"read": 0, "write": 1, "admin": 2}
VALID_SCOPES = tuple(SCOPE_LEVELS.keys())
DEFAULT_NEW_TOKEN_SCOPE = "read"   # least privilege by default for NEW tokens

# Routes needing MORE than their HTTP method implies. Everything else derives:
# GET -> read, POST/PUT/DELETE -> write. Keep this set tiny (no 57-row table).
ADMIN_ROUTES = frozenset({
    ("POST", "/api/tokens"),
    ("GET", "/api/tokens"),
    ("DELETE", "/api/tokens"),
    ("DELETE", "/api/asset-delete"),
    ("POST", "/api/assets/delete-orphaned"),
})


def required_level(method: str, path: str) -> str:
    """Minimum scope a token needs for (method, path)."""
    if (method, path) in ADMIN_ROUTES:
        return "admin"
    return "read" if method == "GET" else "write"


def is_auth_enabled() -> bool:
    """Read at call time (not import) so a flag flip takes effect without a
    re-import and the gate stays testable. The deployed template sets
    AUTH_ENABLED=true (enforcement on by default); an unset var falls back to
    off for local/test runs. Disabling is a deliberate, reversible step (#4)."""
    return os.environ.get("AUTH_ENABLED", "false").lower() == "true"


# Cognito signing keys (JWKS), fetched once over HTTPS and cached per warm
# Lambda. Verification itself is offline CPU work (pure-Python RSA, no native
# crypto and no Cognito API call) after warmup. See ADR #65.
_jwks_keys = None  # dict: kid -> rsa.PublicKey


def _b64url_decode(segment: str) -> bytes:
    """Decode a base64url segment (JWT parts/JWK values carry no padding)."""
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _fetch_jwks() -> dict:
    region = os.environ.get("REGION", "")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not region or not pool_id:
        raise AuthError("cognito not configured")
    url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310 (fixed AWS https URL)
        data = json.loads(resp.read())
    keys = {}
    for jwk in data.get("keys", []):
        if jwk.get("kty") != "RSA" or not jwk.get("kid"):
            continue
        n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
        e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
        keys[jwk["kid"]] = rsa.PublicKey(n, e)
    return keys


def _get_signing_key(kid: str) -> "rsa.PublicKey":
    """Return the RSA public key for `kid`, refetching once on a cache miss to
    tolerate Cognito key rotation."""
    global _jwks_keys
    if _jwks_keys is None:
        _jwks_keys = _fetch_jwks()
    if kid not in _jwks_keys:
        _jwks_keys = _fetch_jwks()
    key = _jwks_keys.get(kid)
    if key is None:
        raise AuthError("unknown signing key")
    return key


class AuthError(Exception):
    """Raised on any auth failure; the gate maps this to HTTP 401."""


class AuthzError(Exception):
    """Raised when authenticated but the token's scope is insufficient; the gate
    maps this to HTTP 403 (ADR #66)."""


class Principal:
    """Who is making the request. Attached to the event on success."""

    __slots__ = ("kind", "subject", "token_name", "scope")

    def __init__(self, kind: str, subject: str, token_name: Optional[str] = None,
                 scope: Optional[str] = None):
        self.kind = kind            # "user" (Cognito) | "service" (PAT)
        self.subject = subject      # cognito sub/username, or token_id
        self.token_name = token_name
        # Cognito operator gets full access; PAT carries its stored scope (a
        # legacy PAT minted before scopes has none -> admin, for compat / #4).
        self.scope = "admin" if kind == "user" else (scope or "admin")


# --- PAT primitives ---------------------------------------------------------

def generate_pat() -> Tuple[str, str]:
    """Return (plaintext_token, sha256_hash).

    Plaintext is shown to the user once at creation and never stored — only
    the hash is persisted, so a DB leak does not leak usable tokens.
    """
    raw = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)
    return raw, hash_token(raw)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def looks_like_pat(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX)


def is_public_path(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


# --- verification -----------------------------------------------------------

def _extract_bearer(event: dict) -> str:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "") or ""
    if not auth.lower().startswith("bearer "):
        raise AuthError("missing bearer token")
    token = auth[len("bearer "):].strip()
    if not token:
        raise AuthError("empty bearer token")
    return token


def verify_pat(token: str, repo) -> Principal:
    """Verify a polyris PAT against the hash store. Checks revoke + expiry."""
    digest = hash_token(token)
    rec = repo.get_by_hash(digest)
    # Constant-time compare even though the lookup was by hash — defends the
    # comparison path against timing oracles.
    if rec is None or not hmac.compare_digest(rec.get("token_hash", ""), digest):
        raise AuthError("invalid token")
    if rec.get("revoked"):
        raise AuthError("token revoked")
    expires_at = rec.get("expires_at")
    if expires_at and expires_at < _now_iso():
        raise AuthError("token expired")
    # Best-effort usage telemetry. PAT volume is low (scripts/CI) — the
    # high-frequency UI uses Cognito, not PATs — so this write is cheap.
    repo.touch_last_used(rec["token_id"])
    return Principal("service", rec["token_id"], rec.get("name"), scope=rec.get("scope"))


def verify_cognito_token(token: str) -> Principal:
    """Verify a Cognito access/ID token offline and bind it to this deployment's
    app client. Pure-Python RS256 (rsa.verify against the pool JWKS) — no native
    crypto, no Cognito API call. See ADR #65."""
    region = os.environ.get("REGION", "")
    pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    if not region or not pool_id or not client_id:
        raise AuthError("auth not configured")

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed token")
    header_b64, payload_b64, sig_b64 = parts

    try:
        header = json.loads(_b64url_decode(header_b64))
    except Exception:
        raise AuthError("malformed token")

    # Pin the algorithm to RS256 BEFORE touching the key — never honour the
    # token's own claim of "none"/HS* (classic alg-confusion defense).
    if header.get("alg") != "RS256":
        raise AuthError("unexpected token algorithm")
    kid = header.get("kid")
    if not kid:
        raise AuthError("missing key id")

    # Verify the RSA signature over `header.payload` with the pool's public key.
    signing_key = _get_signing_key(kid)
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        used_hash = rsa.verify(signing_input, _b64url_decode(sig_b64), signing_key)
    except Exception as e:  # rsa.VerificationError, bad base64, etc.
        # Do not leak the reason to the caller; log it for the operator (#38).
        log.warn("verify_cognito_token", "signature verification failed", error=str(e))
        raise AuthError("invalid token")
    if used_hash != "SHA-256":  # belt-and-braces: RS256 == RSASSA-PKCS1-v1_5 + SHA-256
        raise AuthError("unexpected signature hash")

    try:
        claims = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise AuthError("malformed token")

    # Claim checks (signature is already proven above).
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or time.time() > exp + 60:  # 60s skew leeway
        raise AuthError("token expired")
    if claims.get("iss") != f"https://cognito-idp.{region}.amazonaws.com/{pool_id}":
        raise AuthError("bad issuer")
    if claims.get("token_use") not in ("access", "id"):
        raise AuthError("invalid token use")
    # Cognito access tokens carry `client_id`; ID tokens carry `aud`.
    presented_client = claims.get("client_id") or claims.get("aud")
    if presented_client != client_id:
        raise AuthError("token not issued for this client")

    return Principal("user", claims.get("sub", ""))


def authenticate(event: dict, repo) -> Principal:
    """The gate. One call at the top of handler(); accepts JWT or PAT.

    Wired in main.handler as:

        if is_auth_enabled() and not is_public_path(path):
            try:
                event['principal'] = authenticate(event, api_tokens_repo)
            except AuthError as e:
                return error_response(401, 'UNAUTHORIZED', str(e), request_id=request_id)
    """
    token = _extract_bearer(event)
    if looks_like_pat(token):
        return verify_pat(token, repo)
    return verify_cognito_token(token)


def authorize(principal: Principal, method: str, path: str) -> None:
    """Scope check (ADR #66). Runs in the gate AFTER authenticate(), so identity
    is established. Raises AuthzError (-> 403) if the principal's scope is below
    what the route requires."""
    need = required_level(method, path)
    if SCOPE_LEVELS.get(principal.scope, 0) < SCOPE_LEVELS[need]:
        raise AuthzError(f"requires '{need}' scope, token has '{principal.scope}'")


def _now_iso() -> str:
    # Kept local on purpose: auth is the low-dependency security gate and must
    # not import utils (which pulls in dal/config). One stdlib line is cheaper
    # than that coupling, and importing utils here would also create a cycle
    # (utils -> dal -> api_tokens_repo -> utils). The shared helper used by the
    # repos/routes lives in utils.now_iso().
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
