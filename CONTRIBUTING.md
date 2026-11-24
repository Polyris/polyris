# Contributing to polyris

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11+ | All Lambdas run on 3.12 |
| Node.js | 22+ | LTS recommended; minimum 20.19, which Vite needs |
| AWS SAM CLI | latest | To deploy your checkout to a dev account |
| Make | any | For convenience commands |

No AWS credentials needed for running tests locally (uses pytest-mock `mocker` fixture).

### Key Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| boto3 | latest | AWS SDK |
| pytest | 7.0+ | Testing |
| pytest-mock | 3.0+ | `mocker` fixture for patching |
| jsonata | 2.0+ (npm) | SFN template expression tests |
| next | 16.x | UI framework (App Router) |
| react | 19.x | UI library |
| vitest | 4.x | UI testing |

---

## Quick Start for Developers

### 1. Clone and Setup

```bash
git clone https://github.com/Polyris/polyris.git
cd polyris

# Python dependencies (for SDK development)
pip install -e ".[dev]"

# UI dependencies
cd ui && npm install && cd ..
```

### 2. Run All Tests

```bash
# Everything at once
make test

# Or by area:
make test-sdk           # pytest tests/sdk/
make test-lambdas       # every Lambda suite: console_api, coordination, bucket_cleanup, …
make test-sfn-jsonata   # JSONata expression tests (requires Node.js)
make test-ui            # vitest (requires npm install in ui/)
```

### 3. Before Committing

```bash
make check   # lint, generated-code drift, typecheck, tests
```

---

## Running the UI Locally

```bash
cd ui

# Copy environment template
cp .env.example .env.local

# Edit .env.local — full invoke URL incl. stage and /api:
# NEXT_PUBLIC_API_URL=https://xxx.execute-api.us-east-1.amazonaws.com/dev/api
# A stack has Cognito auth on by default, so sign in as the console does:
# NEXT_PUBLIC_AUTH_ENABLED=true
# NEXT_PUBLIC_COGNITO_USER_POOL_ID=<CognitoUserPoolId stack output>
# NEXT_PUBLIC_COGNITO_CLIENT_ID=<CognitoClientId stack output>
# NEXT_PUBLIC_COGNITO_REGION=us-east-1

# Start dev server
npm run dev
# → http://localhost:3000
```

**Note:** the UI needs a deployed API: `NEXT_PUBLIC_API_URL` is the stack output `ConsoleApiUrl` followed by `/api`. With `NEXT_PUBLIC_AUTH_ENABLED=false` against a stack whose auth is on, every call answers 401.

---

## Project Structure

```
polyris/
├── polyris/              # Python SDK - the DSL users write pipelines with
│   ├── dag.py            # DAG class
│   ├── task.py           # @task decorators
│   ├── generators.py     # Step Functions JSON generation
│   └── ...
│
├── sam/                  # AWS infrastructure
│   ├── lambdas/          # Code of the 8 Lambda functions
│   │   ├── console_api/  # REST API (24 free endpoints)
│   │   ├── coordination/ # Start_Run, the dispatcher, the sweeper and asset fan-out (ADR #137)
│   │   └── _shared/      # logger.py, copied into each Lambda (make sync-loggers)
│   ├── sfn_templates/    # The 2 orchestration definitions: run_task, bulk_backfill
│   └── template.yaml     # SAM template: every AWS resource, 5 SFN definitions (2 orchestration + 3 test)
│
├── ui/                   # React Console UI
│   └── src/
│       ├── components/   # React components
│       ├── hooks/        # Custom hooks (with tests!)
│       └── utils/        # Helpers
│
├── tests/                # SDK, backend, integration and docs tests
├── docs/                 # Documentation
└── examples/             # Example pipelines
```

---

## Key Concepts

### Generated Lambda code

`python -m polyris.codegen.sync_enums` writes `console_api/constants_generated.py` and `ui/src/generated/enums.ts` from `polyris/constants.py`. Never edit them; CI fails on drift (`make check-generate-enums`).

### Deploy your branch to a dev account

Your tests always run your checkout: `pip install -e .` makes `import polyris` read this folder. A deploy does not: `sam build` installs polyris into the console and coordination Lambdas from their `requirements.txt`, which pin the last release. Before you deploy a branch, point both pins at your checkout:

```bash
sed -i "s|^polyris @ .*|polyris @ file://$PWD|" \
  sam/lambdas/console_api/requirements.txt sam/lambdas/coordination/requirements.txt
cd sam && sam build && sam deploy
```

Never commit that change: the path exists only on your machine, so CI and everyone else would fail to build. While it is in place `tests/sdk/test_lambda_sdk_pins.py` fails, as a reminder. Undo it before you commit:

```bash
git checkout -- sam/lambdas/console_api/requirements.txt sam/lambdas/coordination/requirements.txt
```

`scripts/release.sh` moves both pins to the new tag when it cuts a release. [FROM_SOURCE.md](docs/deployment/FROM_SOURCE.md) has the whole deploy.

### Testing Strategy

| Area | Test File | Run With | What it tests |
|------|-----------|----------|---------------|
| SDK core (DAG, tasks, DSL) | `tests/sdk/test_smoke.py` | `pytest tests/sdk/` | DAG structure, dependencies, ASL generation, constants |
| ASL snapshots | `tests/sdk/test_asl_snapshots.py` | `pytest tests/sdk/` | Golden file comparison of generated Step Functions JSON |
| Template features | `tests/sdk/test_templates.py` | `pytest tests/sdk/` | All 24 free console_api routes registered and callable, orchestration_timeout, run_task Catch |
| API routes | `tests/backend/test_api_routes.py` | `pytest tests/backend/` | Route handlers' answers |
| Trigger rules | `tests/sdk/test_coordination_trigger_rules.py` | `pytest tests/sdk/` | The nine trigger rules' truth table, and an unknown rule failing its task |
| SFN flow graphs | `tests/sdk/test_sfn_flow.py` | `pytest tests/sdk/` | State references, reachability, Catch, cycles for every template in `sam/sfn_templates/` |
| JSONata expressions | `tests/sfn_jsonata/` | `cd tests/sfn_jsonata && npm test` | Default fallbacks, type conversions, conditionals, every expression compiles |
| coordination Lambdas | `sam/lambdas/coordination/tests/` | `make test-lambdas` | The dispatcher, start-run, asset-fanout, the sweeper, IAM grants against recorded calls |
| Coordination scenarios | `tests/backend/coordination/` | `pytest tests/backend/coordination/` | Whole runs through the real SFN templates and Lambdas on moto |
| console_api Lambda | `sam/lambdas/console_api/tests/` | `make test-lambdas` | API routes, query/scan pagination, validation |
| UI hooks | `ui/src/hooks/*.test.{ts,tsx}` | `npm test` | React hooks, state management |

**Note:** `pip install -e ".[dev]"` installs pytest, pytest-cov, pytest-mock, moto, ruff, mypy and the boto3 type stubs; the Lambda suites need nothing more.

---

## Common Tasks

> **Open-core & skills.** polyris is open-core with **three tiers**: free (open-source), **Team** and **Enterprise** (both paid). Proprietary code lives per tier under `ui/src/ee/{team,enterprise}/` and `console_api/ee/{team,enterprise}/`; everything else is public and the public build ships without `ee/`. Two boundaries: **free↔paid** is a physical strip; **Team↔Enterprise** is a runtime entitlement (`POLYRIS_TIER` + `can()`). Before adding a feature, decide the tier (authoring, basic read, running and live-run intervention = free; operations and observability = Team; governance/cost/SSO = Enterprise). The checklists below cover the free (public) surface. The **`add-aws-service`** skill in [`.claude/skills/`](.claude/skills) walks adding a new AWS service to the DSL. See ADR #97 (plugin route registration), #98 (open-core split structure), and #99 (UI open-core exclusion) in `docs/reference/` for the mechanism behind the open-core UI surface and API route registration.

### Adding a New Task Status

1. Add the member to `TaskStatus` in `polyris/constants.py`, and to every status set it belongs to
2. Run `python -m polyris.codegen.sync_enums`
3. Add a parity test if another artifact (an SFN template, the UI) reads the value (CLAUDE.md Principle #28)

### Adding a New API Endpoint

Free (public) routes live in `console_api/routes/`. In short:

1. Add a `register(router)` module with `router.add(METHOD, path, handler)`
2. Wire it into `ROUTE_MODULES` in `console_api/main.py`
3. Tests with `pytest-mock` in `console_api/tests/`
4. Document in `docs/operations/API.md`

The plugin registration pattern is described in ADR #97. (Paid Team/Enterprise routes follow the same `register(router)` seam in the private repo.)

### Modifying Step Functions

1. Edit the template in `sam/sfn_templates/<name>/sfn.tpl.json`
2. Run graph validation: `pytest tests/sdk/test_sfn_flow.py`
3. Run the JSONata tests: `make test-sfn-jsonata`
4. Run whole runs through the templates: `pytest tests/backend/coordination/`
5. Run the Lambda suites: `make test-lambdas`
6. Update ASL snapshots if needed: `SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots.py`
7. Deploy your checkout to a dev account ([FROM_SOURCE.md](docs/deployment/FROM_SOURCE.md)) and run the coordination smoke: `make e2e-coordination STACK=<stack> PROFILE=<profile>`

---

## Sign your commits (GPG)

Every commit must carry a GPG signature.

```bash
# One-time setup: tell git which key to use
git config --global user.signingkey <YOUR_KEY_ID>
git config --global commit.gpgsign true

# Then commit normally — signing happens automatically
git commit -m "feat: my change"

# Or sign a single commit explicitly
git commit -S -m "feat: my change"

# Retro-fit signatures on an existing branch
git rebase --exec 'git commit --amend --no-edit -S' origin/main
```

To find your key ID: `gpg --list-secret-keys --keyid-format=long`

---

## PR Checklist

- [ ] Commits GPG-signed — see [GPG signing](#sign-your-commits-gpg)
- [ ] Tests pass locally (`make test`)
- [ ] Generated code in sync (`make check-generate-enums`)
- [ ] No lint errors (`make lint`)
- [ ] CHANGELOG.md updated
- [ ] Documentation updated (if API changed)

---

## Getting Help

- **Code of Conduct:** See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) — all contributors are expected to follow it.
- **Reporting a vulnerability:** See [SECURITY.md](SECURITY.md) — please report security issues privately, not via public issues.
- **Architecture questions:** See `docs/architecture/`
- **API reference:** See `docs/operations/API.md`
- **Questions & discussion:** [GitHub Issues](https://github.com/Polyris/polyris/issues)
