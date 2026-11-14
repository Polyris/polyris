# Polyris E2E Tests

End-to-end tests that hit the **real deployed API**. Zero mocking.

## Quick Start

```bash
# Read-only tests (safe to run against prod)
POLYRIS_API_URL=https://your-api.execute-api.us-east-1.amazonaws.com \
  pytest tests/e2e/ -v -m "not write"

# All tests including write operations
POLYRIS_API_URL=https://your-api.execute-api.us-east-1.amazonaws.com \
POLYRIS_ID_TOKEN=eyJra... \
  pytest tests/e2e/ -v

# Just health checks (deployment smoke test)
POLYRIS_API_URL=https://... pytest tests/e2e/test_health.py -v
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `POLYRIS_API_URL` | ✅ | Console API endpoint (no trailing slash) |
| `POLYRIS_ID_TOKEN` | Only if auth enabled | Bearer credential — a polyris **PAT** (recommended) or a Cognito token |

## Authentication (PAT recommended)

When `AUTH_ENABLED=true`, write/`smoke` tests need a bearer credential in
`POLYRIS_ID_TOKEN`. The gate accepts either a polyris Personal Access Token
(`plrs_…`) or a Cognito token — **a PAT is simpler** and needs no Cognito dance:

```bash
# Generate a PAT once (Console → avatar → API Tokens, or while AUTH is still off):
curl -X POST "$POLYRIS_API_URL/api/tokens" \
  -H "Content-Type: application/json" -d '{"name":"e2e","expires_in_days":90}'
# -> copy the "token" field (shown once)

export POLYRIS_ID_TOKEN=plrs_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
pytest tests/e2e/ -v -m smoke
```

Store the PAT as a CI secret. See `docs/features/api-tokens.md`.

## Test Organization

| File | Scope | Side Effects |
|------|-------|-------------|
| `test_health.py` | Health & metrics | None (read-only) |
| `test_pipelines.py` | Pipeline CRUD | Write tests marked `@pytest.mark.write` |
| `test_assets.py` | Asset queries | None (read-only) |
| `test_routes.py` | Tasks, runs, notifications, error handling | None (read-only) |
| `test_coordination_smoke.py` | Task coordination end to end: `wait_for`, Mark Successful, restart, asset-scheduled runs | Starts runs of the smoke pipelines (`write`, `smoke`) |

## Markers

- No marker = **read-only**, safe to run against any environment
- `@pytest.mark.write` = **mutates state** (triggers runs, modifies configs)

```bash
# Only read-only tests
pytest tests/e2e/ -m "not write"

# Only write tests
pytest tests/e2e/ -m write
```

## Coordination smoke (release gate)

`test_coordination_smoke.py` drives real runs on a dev platform: tasks waiting on assets, Mark Successful, a restart, and pipelines scheduled on assets (ADR #130). It needs the pipelines in `coordination_smoke/dag.py`, deployed once:

```bash
export POLYRIS_SMOKE_SFN_PREFIX=arn:aws:states:us-east-1:123456789012:stateMachine:acme-dev-polyris
cd tests/e2e/coordination_smoke && polyris-deploy --stage dev --profile <profile>
```

The prefix names the platform's own test state machines (`<prefix>-test-quick`, `-test-success`, `-test-failure`), which the smoke tasks run. Then, from the repository root:

```bash
POLYRIS_API_URL=https://... POLYRIS_ID_TOKEN=plrs_... pytest tests/e2e/test_coordination_smoke.py -v
```

It takes about 15 minutes. Each scenario uses its own run date in the 1900s, so the facts of earlier runs never decide a new one.

## Getting a Cognito Token (fallback — prefer a PAT, see above)

```bash
# admin-initiate-auth is the reliable admin path (polyris uses SRP +
# admin-only users, so USER_PASSWORD_AUTH is usually not enabled on the client):
aws cognito-idp admin-initiate-auth \
  --user-pool-id YOUR_POOL_ID \
  --client-id YOUR_CLIENT_ID \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=you@example.com,PASSWORD=yourpass \
  --query 'AuthenticationResult.AccessToken' --output text
```

## CI Integration

Add to your CI pipeline after deploy:

```yaml
- name: E2E smoke test
  run: |
    POLYRIS_API_URL=${{ steps.deploy.outputs.api_url }} \
    pytest tests/e2e/test_health.py -v
```

## Design Decisions

- **stdlib only** — no requests/httpx dependency, uses `urllib` so tests run anywhere Python runs
- **session-scoped fixtures** — pipeline list fetched once, shared across tests
- **graceful skip** — tests skip automatically if `POLYRIS_API_URL` is not set
- **no test data creation** — read-only tests work with whatever is deployed; write tests are opt-in
