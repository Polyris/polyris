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
| `POLYRIS_ID_TOKEN` | Only if auth enabled | A Cognito token of a console user of this deployment |

## Authentication

When the platform has Cognito auth, write and `smoke` tests need a token in `POLYRIS_ID_TOKEN`. The console's app client allows `ADMIN_USER_PASSWORD_AUTH`, which works only with AWS credentials allowed `cognito-idp:AdminInitiateAuth`. `scripts/get-e2e-token.sh` signs in a console user that has a permanent password:

```bash
export COGNITO_USER_POOL_ID=us-east-1_xxxxx COGNITO_CLIENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxx
export E2E_USERNAME=you@example.com E2E_PASSWORD='…'
export POLYRIS_ID_TOKEN="$(scripts/get-e2e-token.sh)"
```

The pool and client ids are the platform stack's `CognitoUserPoolId` and `CognitoClientId` outputs. A token lasts an hour. The coordination smoke below needs none of this: it signs in by itself.

## Test Organization

| File | Scope | Side Effects |
|------|-------|-------------|
| `test_health.py` | Health & metrics | None (read-only) |
| `test_pipelines.py` | Pipeline CRUD | Write tests marked `@pytest.mark.write` |
| `test_routes.py` | Tasks, runs, notifications, error handling | None (read-only) |
| `test_retries.py` | `retry_attempt` on task rows and retry events (ADR #125) | None (read-only) |
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

`test_coordination_smoke.py` drives real runs on a dev platform: tasks waiting on assets, Mark Successful, a restart, and pipelines scheduled on assets (ADR #130). One command runs it, from the repository root:

```bash
make e2e-coordination STACK=<platform stack> PROFILE=<aws profile>
```

`tests/e2e/run_coordination_smoke.py` reads the console URL, the Cognito pool and the platform's test state machines from the stack's outputs. It deploys the pipelines of `coordination_smoke/dag.py` with `polyris-deploy`, named after the platform, so they run the platform's `-test-quick`, `-test-success` and `-test-failure` state machines. It signs in as a temporary console user, runs the test and deletes the user again. Your AWS credentials need `cloudformation:DescribeStacks`, what `polyris-deploy` needs ([IAM_PERMISSIONS.md](../../docs/deployment/IAM_PERMISSIONS.md)), and `cognito-idp:AdminCreateUser`, `AdminSetUserPassword`, `AdminInitiateAuth` and `AdminDeleteUser`. `ARGS` go to pytest: `ARGS='-k marking'` reruns one scenario.

It takes about 15 minutes. The pipelines stay on the platform, so the next run only updates them; they have no schedule and cost nothing between runs. Remove them before `polyris-down`, which refuses a platform that still has pipelines:

```bash
make e2e-coordination-clean STACK=<platform stack> PROFILE=<aws profile>
```

Run data needs no clean-up: each scenario uses its own date in the 1900s, and the rows expire with the tables' TTL. Run the smoke on a test platform, not on one people use: it adds nine pipelines to the console. Each scenario uses its own run date in the 1900s, so the facts of earlier runs never decide a new one.

A task that fails waits in `waiting_decision` until the decision timeout, and only then becomes `failed`; a scenario that needs a failed task accepts either status.

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
