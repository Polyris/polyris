"""Who sees what of the public health routes.

GET /api/health shows a caller without a valid token only each check's status
word; a signed-in caller, or anyone while auth is off, gets the details and
the findings of the console's System status. /api/metrics never answers with
error text, which names AWS resources and the account.

The external boundaries only are replaced: DynamoDB and Step Functions are
moto, with the tables of sam/template.yaml, behind the console's own lazy
clients; a call AWS denies is a 403 that botocore itself turns into the
ClientError (`_deny_every_call`); Cognito's key endpoint is
`urllib.request.urlopen`. main.handler, the auth gate, PAT hashing, JWT
verification and every check run for real.
"""
import base64
import importlib.util
import io
import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import rsa
from moto import mock_aws

from polyris.coordination import sweep
from polyris.coordination.times import format_utc

_KIT_PATH = Path(__file__).resolve().parents[3] / "coordination" / "tests" / "coordination_testkit.py"
_spec = importlib.util.spec_from_file_location("coordination_testkit", _KIT_PATH)
assert _spec and _spec.loader
kit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(kit)

POOL, CLIENT = "us-east-1_pool", "client123"
_PUBLIC, _PRIVATE = rsa.newkeys(1024)

#: What an AccessDenied names: the account, the caller's role and function, and the request.
ACCOUNT = "987654321098"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/acme-prod-polyris-console-api-role"
FUNCTION = "acme-prod-polyris-console-api"
REQUEST = "5f0c2e3a-9b1d-4c7e-8a2f-6d3b1e9c4a70"
DENIED = (f"User: arn:aws:sts::{ACCOUNT}:assumed-role/acme-prod-polyris-console-api-role/{FUNCTION} is not "
          f"authorized to perform this action with an explicit deny in an identity-based policy: {ROLE} "
          f"(Request ID: {REQUEST})")
SECRETS = (ACCOUNT, ROLE, FUNCTION, REQUEST)

#: The names the sweeper writes into its report.
NAMES = {"sweeper_function": "acme-prod-polyris-sweeper", "fanout_function": "acme-prod-polyris-asset-fanout",
         "events_table": "acme-prod-polyris-asset-events",
         "fanout_mapping_uuid": "48529b7b-abda-4306-8550-8d3bbc1391c3", "region": "eu-west-1",
         "dispatcher_function": "acme-prod-polyris-reconcile", "tasks_table": "acme-prod-polyris-pipeline-tokens",
         "dispatch_mapping_uuid": "1f2e3d4c-5b6a-4978-8a9b-0c1d2e3f4a5b"}
CHECKS = {"dynamodb", "stepfunctions", "recent_failures", "circuit_breakers", "coordination"}
WORDS = {"healthy", "degraded", "unhealthy"}


@pytest.fixture
def aws(mocker, monkeypatch):
    """moto behind the console's lazy clients, with its tables from sam/template.yaml; auth on."""
    import config
    from dal import api_tokens_repo, executions_repo

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("REGION", "us-east-1")
    monkeypatch.setenv("COGNITO_USER_POOL_ID", POOL)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT)
    with mock_aws():
        mocker.patch.object(config, "_dynamodb", None)       # the console creates its clients again, inside moto
        mocker.patch.object(config, "_sfn", None)
        kit.create_tables(config.dynamodb, {"PipelineTokensTable": executions_repo._table_name,
                                            "ApiTokensTable": api_tokens_repo._table_name})
        yield config


@pytest.fixture
def jwks(mocker, monkeypatch):
    """The pool's signing key, served where auth.py fetches it."""
    import auth

    monkeypatch.setattr(auth, "_jwks_keys", None)              # nothing cached by an earlier test
    document = json.dumps({"keys": [{"kty": "RSA", "kid": "k1", "n": _b64_int(_PUBLIC.n),
                                     "e": _b64_int(_PUBLIC.e)}]}).encode()
    return mocker.patch("urllib.request.urlopen", side_effect=lambda *a, **kw: io.BytesIO(document))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_int(value: int) -> str:
    return _b64(value.to_bytes((value.bit_length() + 7) // 8, "big"))


def _jwt(*, expires_in: int) -> str:
    """A Cognito access token of this pool and client, signed with the pool's key."""
    header = _b64(json.dumps({"alg": "RS256", "kid": "k1", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"sub": "user-1", "token_use": "access", "client_id": CLIENT,
                              "iss": f"https://cognito-idp.us-east-1.amazonaws.com/{POOL}",
                              "exp": int(time.time()) + expires_in}).encode())
    signature = rsa.sign(f"{header}.{claims}".encode("ascii"), _PRIVATE, "SHA-256")
    return f"{header}.{claims}.{_b64(signature)}"


def _pat(*, revoked: bool = False) -> str:
    """A personal access token minted and stored as the tokens route does: only its hash is kept."""
    from auth import generate_pat
    from dal import api_tokens_repo

    plaintext, digest = generate_pat()
    api_tokens_repo.put({"token_id": f"tok_{digest[:8]}", "token_hash": digest, "name": "monitor",
                         "scope": "read", "revoked": revoked})
    return plaintext


def _engine(**fields) -> None:
    """A sweeper report that names every resource: silent for 30 minutes, both mappings and both streams off."""
    from dal import executions_repo

    executions_repo.put({"execution_name": sweep.HEALTH_KEY,
                         "checked_at": format_utc(datetime.now(timezone.utc) - timedelta(minutes=30)),
                         "fanout_mapping": "Disabled", "fanout_result": "OK", "stream": "DISABLED",
                         "dispatch_mapping": "Disabled", "tasks_stream": "DISABLED", "oldest_overdue": "", "failed_steps": [], **NAMES, **fields})


def _get(path: str = "/api/health", token=None) -> dict:
    import main

    event = {"rawPath": path, "headers": {"Authorization": f"Bearer {token}"} if token else {},
             "queryStringParameters": None,
             "requestContext": {"http": {"method": "GET"}, "requestId": "r1", "stage": "$default"}}
    return main.handler(event, None)


class _Forbidden:
    """The HTTP answer to a denied call; botocore reads its status code."""
    status_code = 403


def _deny_every_call(client) -> None:
    """Every call of `client` is denied by IAM, with an error text that names the caller."""
    def deny(**_):
        return _Forbidden(), {"Error": {"Code": "AccessDeniedException", "Message": DENIED},
                              "ResponseMetadata": {"HTTPStatusCode": 403}}

    client.meta.events.register("before-call", deny)


def _logged_errors(capsys, fn: str) -> list:
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    return [line for line in lines if line.get("level") == "ERROR" and line.get("fn") == fn]


# ── (a) the minimal body ────────────────────────────────────────────────────

def test_without_a_token_the_body_holds_exactly_each_checks_status_word(aws):
    _engine()
    anonymous, signed_in = _get(), _get(token=_pat())
    minimal, full = json.loads(anonymous["body"]), json.loads(signed_in["body"])

    assert set(minimal) == {"status", "timestamp", "response_time_ms", "detail", "checks"}
    assert minimal["detail"] == "minimal"
    assert set(minimal["checks"]) == set(full["checks"]) == CHECKS
    for name, check in minimal["checks"].items():
        assert check == {"status": full["checks"][name]["status"]}, name
        assert check["status"] in WORDS, name
    assert (anonymous["statusCode"], minimal["status"]) == (signed_in["statusCode"], full["status"])
    # The report's names are in the details, never in the minimal body.
    for name in NAMES.values():
        assert name in signed_in["body"] and name not in anonymous["body"], name


# ── (b) the canary: AWS error text ──────────────────────────────────────────

def test_aws_error_text_never_reaches_a_caller_without_a_token(aws, mocker, monkeypatch):
    """Every check fails with an AccessDenied whose text names the account, a role, a function and a request."""
    from dal import circuit_breakers_repo

    mocker.patch.object(circuit_breakers_repo, "_table_name", "test-circuit-breakers")   # the optional check runs too
    _deny_every_call(aws.dynamodb.meta.client)
    _deny_every_call(aws.sfn)

    anonymous = _get()

    assert anonymous["statusCode"] == 503
    assert json.loads(anonymous["body"])["detail"] == "minimal"
    for secret in SECRETS:
        assert secret not in anonymous["body"], secret
    # The same answer with auth off carries the text in every check: the assertions above test the real leak.
    monkeypatch.setenv("AUTH_ENABLED", "false")
    full = json.loads(_get()["body"])
    assert all(DENIED in json.dumps(full["checks"][name]) for name in CHECKS - {"coordination"})
    assert [f["title"] for f in full["findings"]] == [
        "The coordination status cannot be read", "The console cannot reach DynamoDB",
        "The console cannot reach Step Functions"]
    assert all(DENIED in f["resource"] for f in full["findings"])


# ── (c) a missing or invalid token ──────────────────────────────────────────

CREDENTIALS = {
    "no token": lambda: None,
    "a garbage token": lambda: "not-a-token",
    "an expired JWT": lambda: _jwt(expires_in=-3600),
    "a revoked PAT": lambda: _pat(revoked=True),
    "an unknown PAT": lambda: "plrs_" + "u" * 43,
}


@pytest.mark.parametrize("credential", list(CREDENTIALS))
def test_a_missing_or_invalid_token_gets_the_minimal_body_never_401_or_500(aws, jwks, capsys, credential):
    _engine()
    resp = _get(token=CREDENTIALS[credential]())

    assert resp["statusCode"] in (200, 503)
    assert json.loads(resp["body"])["detail"] == "minimal"
    assert _logged_errors(capsys, "optional_principal") == []          # an invalid token is not an error


# ── (d) a valid token, or auth off ──────────────────────────────────────────

def test_a_valid_pat_gets_the_details_and_the_findings(aws):
    _engine()
    resp = _get(token=_pat())

    body = json.loads(resp["body"])
    assert (resp["statusCode"], body["status"], body["detail"]) == (200, "degraded", "full")
    assert [f["title"] for f in body["findings"]] == [
        "The periodic re-check is not running", "Asset event delivery is off",
        "Asset events are not recorded for delivery", "Task delivery is off",
        "Task changes are not recorded for delivery"]
    assert body["findings"][1]["command"] == (
        "aws lambda update-event-source-mapping --uuid 48529b7b-abda-4306-8550-8d3bbc1391c3 --enabled "
        "--region eu-west-1")
    coordination = body["checks"]["coordination"]
    assert (coordination["status"], set(coordination)) == ("unhealthy", {"status", "report"})


def test_a_valid_cognito_token_gets_the_details(aws, jwks):
    _engine()
    body = json.loads(_get(token=_jwt(expires_in=3600))["body"])
    assert body["detail"] == "full" and len(body["findings"]) == 5
    assert jwks.call_count == 1                                     # verified against the pool's key


def test_with_auth_off_anyone_gets_the_details(aws, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    _engine(checked_at=format_utc(datetime.now(timezone.utc)), fanout_mapping="Enabled", stream="ENABLED",
            dispatch_mapping="Enabled", tasks_stream="ENABLED")

    resp = _get()

    body = json.loads(resp["body"])
    assert (resp["statusCode"], body["status"], body["detail"], body["findings"]) == (200, "healthy", "full", [])


# ── (e) a token that cannot be verified ─────────────────────────────────────

def test_a_jwks_fetch_that_fails_gets_the_minimal_body(aws, mocker, monkeypatch, capsys):
    import auth

    monkeypatch.setattr(auth, "_jwks_keys", None)
    mocker.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out"))
    _engine()

    resp = _get(token=_jwt(expires_in=3600))

    assert resp["statusCode"] in (200, 503)
    assert json.loads(resp["body"])["detail"] == "minimal"
    [logged] = _logged_errors(capsys, "optional_principal")
    assert "timed out" in logged["error"]


def test_a_token_table_that_cannot_be_read_gets_the_minimal_body(aws, capsys):
    from dal import api_tokens_repo

    pat = _pat()
    aws.dynamodb.Table(api_tokens_repo._table_name).delete()
    _engine()

    resp = _get(token=pat)

    assert resp["statusCode"] in (200, 503)
    assert json.loads(resp["body"])["detail"] == "minimal"
    [logged] = _logged_errors(capsys, "optional_principal")
    assert "ResourceNotFoundException" in logged["error"]


# ── (f) caching ─────────────────────────────────────────────────────────────

def test_no_view_may_be_stored_by_a_cache(aws):
    _engine()
    for token, detail in ((None, "minimal"), (_pat(), "full")):
        resp = _get(token=token)
        assert json.loads(resp["body"])["detail"] == detail
        assert "no-store" in resp["headers"]["Cache-Control"], detail
    assert "no-store" in _get("/api/health/live")["headers"]["Cache-Control"]


# ── (g) a 503 ───────────────────────────────────────────────────────────────

def test_an_unreachable_dynamodb_is_a_503_whose_full_body_names_it(aws):
    from dal import executions_repo

    pat = _pat()
    aws.dynamodb.Table(executions_repo._table_name).delete()       # every read of the tokens table fails

    resp = _get(token=pat)

    body = json.loads(resp["body"])
    assert (resp["statusCode"], body["status"], body["detail"]) == (503, "unhealthy", "full")
    unreadable, unreachable = body["findings"]
    assert unreadable["title"] == "The coordination status cannot be read"
    assert unreadable["resource"].startswith("ResourceNotFoundException: ")
    assert unreachable == {
        "title": "The console cannot reach DynamoDB",
        "resource": body["checks"]["dynamodb"]["message"],
        "impact": "The console cannot read or change pipelines and tasks.",
        "fix": "Check the console API's role and the AWS Health Dashboard, then read the console API's logs.",
    }
    anonymous = _get()
    assert (anonymous["statusCode"], json.loads(anonymous["body"])["detail"]) == (503, "minimal")


def test_an_unreachable_step_functions_is_a_503_whose_full_body_names_it(aws):
    _engine(checked_at=format_utc(datetime.now(timezone.utc)), fanout_mapping="Enabled", stream="ENABLED",
            dispatch_mapping="Enabled", tasks_stream="ENABLED")
    _deny_every_call(aws.sfn)

    resp = _get(token=_pat())

    body = json.loads(resp["body"])
    assert (resp["statusCode"], body["status"]) == (503, "unhealthy")
    assert body["findings"] == [{
        "title": "The console cannot reach Step Functions",
        "resource": body["checks"]["stepfunctions"]["message"],
        "impact": "The console cannot start or stop runs.",
        "fix": "Check the console API's role and the AWS Health Dashboard, then read the console API's logs.",
    }]
    assert DENIED in body["findings"][0]["resource"]


# ── /api/metrics and /api/health/live ───────────────────────────────────────

def test_metrics_answer_without_aws_error_text_and_log_it(aws, capsys):
    from routes.health import METRICS_UNAVAILABLE

    _deny_every_call(aws.dynamodb.meta.client)

    resp = _get("/api/metrics")

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["metrics"] == {"tasks": {"error": METRICS_UNAVAILABLE},
                                                   "pipelines": {"error": METRICS_UNAVAILABLE}}
    for secret in SECRETS:
        assert secret not in resp["body"], secret
    logged = _logged_errors(capsys, "get_metrics")
    assert len(logged) == 2 and all(DENIED in line["error"] for line in logged)


def test_liveness_answers_without_any_downstream_call(aws):
    _deny_every_call(aws.dynamodb.meta.client)
    _deny_every_call(aws.sfn)

    resp = _get("/api/health/live")

    assert resp["statusCode"] == 200
    assert set(json.loads(resp["body"])) == {"status", "timestamp"}
    assert json.loads(resp["body"])["status"] == "ok"


@pytest.mark.parametrize("path,shown", [("/api/health/live", False), ("/api/pipelines", True)])
def test_an_unexpected_failure_shows_its_text_only_on_a_route_that_needs_a_caller(aws, mocker, monkeypatch, path, shown):
    """The catch-all hides the error of a public route, and keeps it for a route behind the gate."""
    import main

    monkeypatch.setenv("AUTH_ENABLED", "false")      # the gate lets the private route through without a token

    def boom(_event):
        raise RuntimeError(DENIED)

    mocker.patch.dict(main.ROUTES, {("GET", path): (boom, None)})

    resp = _get(path)

    assert resp["statusCode"] == 500
    assert (DENIED in resp["body"]) is shown
    if not shown:
        assert json.loads(resp["body"])["message"] == main.INTERNAL_ERROR_MESSAGE
