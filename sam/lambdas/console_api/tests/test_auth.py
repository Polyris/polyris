"""
Unit tests for auth.py (ADR #65) — the dual-auth gate.

Mock boundary: the token repo and the cognito client only. Hashing and the
branch/verification logic run for real (#14).
"""

import pytest

import base64
import json
import time

import rsa

# Throwaway RSA keypairs for signing/verifying test JWTs. 1024-bit keeps keygen
# fast while exercising the real RSASSA-PKCS1-v1_5/SHA-256 path. _OTHER_* mints
# "wrong key" tokens for the bad-signature case.
(_PUB, _PRIV) = rsa.newkeys(1024)
(_OTHER_PUB, _OTHER_PRIV) = rsa.newkeys(1024)


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _make_token(priv=None, alg='RS256', kid='testkid', **claim_overrides) -> str:
    """Mint a signed Cognito-style JWT for tests. claim_overrides mutate claims;
    a value of None drops that claim entirely."""
    header = {'alg': alg, 'kid': kid, 'typ': 'JWT'}
    claims = {
        'sub': 'user-sub-123',
        'token_use': 'access',
        'client_id': 'client123',
        'iss': 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool',
        'exp': int(time.time()) + 3600,
    }
    claims.update(claim_overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    header_b64 = _b64u(json.dumps(header).encode())
    payload_b64 = _b64u(json.dumps(claims).encode())
    signing_input = f"{header_b64}.{payload_b64}".encode('ascii')
    sig = rsa.sign(signing_input, priv or _PRIV, 'SHA-256')
    return f"{header_b64}.{payload_b64}.{_b64u(sig)}"


# ── PAT primitives ────────────────────────────────────────────────────────

class TestPatPrimitives:
    def test_generate_pat_has_prefix_and_hash(self):
        from auth import generate_pat, hash_token, TOKEN_PREFIX
        plaintext, digest = generate_pat()
        assert plaintext.startswith(TOKEN_PREFIX)
        assert digest == hash_token(plaintext)
        assert len(digest) == 64  # sha256 hex

    def test_generate_pat_is_unique(self):
        from auth import generate_pat
        a, _ = generate_pat()
        b, _ = generate_pat()
        assert a != b

    def test_looks_like_pat(self):
        from auth import looks_like_pat
        assert looks_like_pat("plrs_abc")
        assert not looks_like_pat("eyJhbGciOiJ...")

    def test_is_public_path(self):
        from auth import is_public_path
        assert is_public_path("/api/health")
        assert is_public_path("/api/health/simple")
        assert is_public_path("/api/metrics")
        assert not is_public_path("/api/pipelines")
        assert not is_public_path("/api/tokens")


# ── PAT verification ──────────────────────────────────────────────────────

class _FakeRepo:
    def __init__(self, rec=None):
        self._rec = rec
        self.touched = []

    def get_by_hash(self, digest):
        return self._rec

    def touch_last_used(self, token_id):
        self.touched.append(token_id)


def _bearer(token):
    return {'headers': {'Authorization': f'Bearer {token}'}}


class TestVerifyPat:
    def test_valid_token(self):
        from auth import verify_pat, hash_token
        plaintext = "plrs_validtoken"
        repo = _FakeRepo({'token_id': 'tok_1', 'token_hash': hash_token(plaintext),
                          'name': 'ci', 'revoked': False})
        principal = verify_pat(plaintext, repo)
        assert principal.kind == 'service'
        assert principal.subject == 'tok_1'
        assert principal.token_name == 'ci'
        assert repo.touched == ['tok_1']  # last_used stamped

    def test_unknown_token_rejected(self):
        from auth import verify_pat, AuthError
        repo = _FakeRepo(None)
        with pytest.raises(AuthError):
            verify_pat("plrs_nope", repo)

    def test_revoked_token_rejected(self):
        from auth import verify_pat, hash_token, AuthError
        plaintext = "plrs_revoked"
        repo = _FakeRepo({'token_id': 'tok_2', 'token_hash': hash_token(plaintext),
                          'revoked': True})
        with pytest.raises(AuthError):
            verify_pat(plaintext, repo)

    def test_expired_token_rejected(self):
        from auth import verify_pat, hash_token, AuthError
        plaintext = "plrs_expired"
        repo = _FakeRepo({'token_id': 'tok_3', 'token_hash': hash_token(plaintext),
                          'revoked': False, 'expires_at': '2000-01-01T00:00:00+00:00'})
        with pytest.raises(AuthError):
            verify_pat(plaintext, repo)


# ── Cognito verification ──────────────────────────────────────────────────

class TestVerifyCognito:
    @staticmethod
    def _env(monkeypatch):
        monkeypatch.setenv('REGION', 'us-east-1')
        monkeypatch.setenv('COGNITO_USER_POOL_ID', 'us-east-1_pool')
        monkeypatch.setenv('COGNITO_CLIENT_ID', 'client123')

    @staticmethod
    def _use_test_key(mocker):
        """Verify against our throwaway public key (skips the JWKS HTTP fetch);
        the signature + claim checks all run for real (#14)."""
        import auth
        mocker.patch.object(auth, '_get_signing_key', return_value=_PUB)

    def test_valid_access_token(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        principal = auth.verify_cognito_token(_make_token())
        assert principal.kind == 'user'
        assert principal.subject == 'user-sub-123'

    def test_id_token_uses_aud(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        tok = _make_token(token_use='id', aud='client123', client_id=None)
        assert auth.verify_cognito_token(tok).subject == 'user-sub-123'

    def test_wrong_client_rejected(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        with pytest.raises(auth.AuthError):
            auth.verify_cognito_token(_make_token(client_id='SOMEONE_ELSE'))

    def test_bad_signature_rejected(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        # Signed by a DIFFERENT key than verification uses -> must be rejected.
        with pytest.raises(auth.AuthError):
            auth.verify_cognito_token(_make_token(priv=_OTHER_PRIV))

    def test_alg_confusion_rejected(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        # 'none' / HS* must never be honoured, regardless of the signature bytes.
        for bad_alg in ('none', 'HS256'):
            with pytest.raises(auth.AuthError):
                auth.verify_cognito_token(_make_token(alg=bad_alg))

    def test_expired_rejected(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        with pytest.raises(auth.AuthError):
            auth.verify_cognito_token(_make_token(exp=int(time.time()) - 3600))

    def test_bad_issuer_rejected(self, mocker, monkeypatch):
        import auth
        self._env(monkeypatch)
        self._use_test_key(mocker)
        with pytest.raises(auth.AuthError):
            auth.verify_cognito_token(_make_token(iss='https://evil.example/pool'))

    def test_not_configured_rejected(self, monkeypatch):
        import auth
        monkeypatch.setenv('REGION', 'us-east-1')
        monkeypatch.delenv('COGNITO_USER_POOL_ID', raising=False)
        with pytest.raises(auth.AuthError):
            auth.verify_cognito_token("eyJ.x.y")

    def test_client_id_not_configured_rejected(self, monkeypatch):
        """Regression test: region and pool_id present but client_id missing
        must fail closed ("auth not configured"), the same as a missing
        pool_id — not silently skip the client-id restriction and accept a
        token from any app client in the pool. In the deployed SAM template
        these three env vars are set/unset together via the same !If
        condition, so this exact combination shouldn't arise there — but the
        code must not depend solely on that external coupling to stay safe."""
        import auth
        monkeypatch.setenv('REGION', 'us-east-1')
        monkeypatch.setenv('COGNITO_USER_POOL_ID', 'us-east-1_pool')
        monkeypatch.delenv('COGNITO_CLIENT_ID', raising=False)
        with pytest.raises(auth.AuthError, match="not configured"):
            auth.verify_cognito_token("eyJ.x.y")


# ── The gate ──────────────────────────────────────────────────────────────

class TestAuthenticate:
    def test_pat_branch(self):
        from auth import authenticate, hash_token
        plaintext = "plrs_gate"
        repo = _FakeRepo({'token_id': 'tok_g', 'token_hash': hash_token(plaintext),
                          'revoked': False})
        principal = authenticate(_bearer(plaintext), repo)
        assert principal.kind == 'service'

    def test_jwt_branch(self, mocker, monkeypatch):
        import auth
        monkeypatch.setenv('REGION', 'us-east-1')
        monkeypatch.setenv('COGNITO_USER_POOL_ID', 'us-east-1_pool')
        monkeypatch.setenv('COGNITO_CLIENT_ID', 'client123')
        mocker.patch.object(auth, '_get_signing_key', return_value=_PUB)
        principal = auth.authenticate(_bearer(_make_token(sub='sub-jwt')), _FakeRepo(None))
        assert principal.kind == 'user'
        assert principal.subject == 'sub-jwt'

    def test_missing_header_rejected(self):
        from auth import authenticate, AuthError
        with pytest.raises(AuthError):
            authenticate({'headers': {}}, _FakeRepo(None))

    def test_non_bearer_rejected(self):
        from auth import authenticate, AuthError
        with pytest.raises(AuthError):
            authenticate({'headers': {'Authorization': 'Basic abc'}}, _FakeRepo(None))
