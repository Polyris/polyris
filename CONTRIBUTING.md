# Contributing to polyris

## Prerequisites

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11+ | All Lambdas run on 3.12 |
| Node.js | 22+ | LTS recommended; minimum 18.18 for Next.js 16 |
| AWS SAM CLI | latest | For infrastructure deployment |
| AWS CLI | 2.x | For local testing |
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
| vitest | 2.x | UI testing |

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
make test-lambdas       # evaluate_deps, check_assets, notify_asset_subscribers, console_api
make test-sfn-jsonata   # JSONata expression tests (requires Node.js)
make test-ui            # vitest (requires npm install in ui/)
```

### 3. Before Committing

```bash
make check   # Runs: lint, sync-constants, test

# Or manually:
make sync-constants
make lint
```

---

## Running the UI Locally

```bash
cd ui

# Copy environment template
cp .env.example .env.local

# Edit .env.local — full invoke URL incl. stage and /api:
# NEXT_PUBLIC_API_URL=https://xxx.execute-api.us-east-1.amazonaws.com/dev/api
# NEXT_PUBLIC_AUTH_ENABLED=false

# Start dev server
npm run dev
# → http://localhost:3000
```

**Note:** UI needs a backend API. Options:
1. Point to a deployed environment (set `NEXT_PUBLIC_API_URL` in `.env.local`; see `docs/operations/UI.md`)
2. Run with mock data (not yet implemented)

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
├── sam/     # AWS Infrastructure
│   ├── lambdas/          # Lambda function code
│   │   ├── console_api/  # REST API (27 free endpoints)
│   │   ├── evaluate_deps/# Dependency evaluation
│   │   ├── check_assets/ # Asset freshness checks
│   │   ├── notify_asset_subscribers/ # Asset event notification
│   │   ├── query_subscriptions/     # Downstream subscriber lookup
│   │   └── _shared/      # Shared constants (SOURCE OF TRUTH)
│   ├── sfn_templates/    # Step Functions ASL templates
│   └── template.yaml    # SAM template
│
├── ui/                   # React Console UI
│   └── src/
│       ├── components/   # React components
│       ├── hooks/        # Custom hooks (with tests!)
│       └── utils/        # Helpers
│
├── tests/                # SDK integration tests
├── docs/                 # Documentation
└── examples/             # Example pipelines
```

---

## Key Concepts

### Lambda Constants Sync

Lambdas can't share code, so `TaskStatus` is duplicated. Keep in sync manually:

```bash
# Check if in sync
make sync-constants

# Fix by copying source of truth
cp sam/lambdas/_shared/constants.py sam/lambdas/evaluate_deps/constants.py
```

**Source of truth:** `sam/lambdas/_shared/constants.py`

### Testing Strategy

| Area | Test File | Run With | What it tests |
|------|-----------|----------|---------------|
| SDK core (DAG, tasks, DSL) | `tests/sdk/test_smoke.py` | `pytest tests/sdk/` | DAG structure, dependencies, ASL generation, constants |
| ASL snapshots | `tests/sdk/test_asl_snapshots.py` | `pytest tests/sdk/` | Golden file comparison of generated Step Functions JSON |
| Template features | `tests/sdk/test_templates.py` | `pytest tests/sdk/` | orchestration_timeout, route table, run_task resilience |
| API routes | `tests/backend/test_api_routes.py` | `pytest tests/backend/` | All 27 free console_api routes registered and callable |
| Trigger rules | `tests/sdk/test_trigger_rules.py` | `pytest tests/sdk/` | 5 trigger rules: Python ↔ JSONata logic sync |
| SFN flow graphs | `tests/sdk/test_sfn_flow.py` | `pytest tests/sdk/` | State references, reachability, Catch, cycles for all 13 templates |
| JSONata expressions | `tests/sfn_jsonata/` | `cd tests/sfn_jsonata && npm test` | Default fallbacks, type conversions, conditionals, 550 expressions compile |
| evaluate_deps Lambda | `sam/lambdas/evaluate_deps/` | `PYTHONPATH=. pytest` | BatchGetItem, trigger_rule evaluation, paused check |
| check_assets Lambda | `sam/lambdas/check_assets/` | `PYTHONPATH=. pytest` | Asset freshness, wait_for logic |
| notify_asset_subscribers | `sam/lambdas/notify_asset_subscribers/` | `PYTHONPATH=. pytest` | Asset event publishing, subscriber notification |
| console_api Lambda | `sam/lambdas/console_api/tests/` | `PYTHONPATH=. pytest` | API routes, query/scan pagination, validation |
| UI hooks | `ui/src/hooks/*.test.{js,jsx}` | `npm test` | React hooks, state management |

**Note:** `pip install -e ".[dev]"` installs: pytest, pytest-cov, ruff, mypy. Lambda tests only need pytest and boto3.

### Local Lambda Development

```bash
cd sam/lambdas/evaluate_deps

# Run tests with mocked DynamoDB
PYTHONPATH=. pytest test_evaluate_deps.py -v

# Test specific function
PYTHONPATH=. pytest test_evaluate_deps.py::TestCheckTriggerRule -v
```

---

## Common Tasks

> **Open-core & skills.** polyris is open-core with **three tiers**: free
> (open-source), **Team** and **Enterprise** (both paid, hosted). Proprietary code
> lives per tier under `ui/src/ee/{team,enterprise}/` and
> `console_api/ee/{team,enterprise}/`; everything else is public and the public
> build ships without `ee/`. Two boundaries: **free↔paid** is a physical strip;
> **Team↔Enterprise** is a runtime entitlement (`POLYRIS_TIER` + `can()`). Before
> adding a feature, decide the tier (authoring/basic-read = free; ops/intervention
> = Team; governance/cost/SSO = Enterprise). The checklists below cover the free
> (public) surface. The **`add-aws-service`** skill in
> [`.claude/skills/`](.claude/skills) walks adding a new AWS service to the DSL.
> See ADR #97 (plugin route registration), #98 (open-core split structure), and
> #99 (UI open-core exclusion) in `docs/reference/` for the mechanism behind the
> open-core UI surface and API route registration.

### Adding a New Task Status

1. Add to `_shared/constants.py`
2. Copy to `evaluate_deps/constants.py`
3. Update `console_api/constants.py` manually if needed
4. Update UI constants in `ui/src/utils/constants.js`
5. Run `make sync-constants` to verify

### Adding a New API Endpoint

Free (public) routes live in `console_api/routes/`. In short:

1. Add a `register(router)` module with `router.add(METHOD, path, handler)`
2. Wire it into `ROUTE_MODULES` in `console_api/main.py`
3. Tests with `pytest-mock` in `console_api/tests/`
4. Document in `docs/operations/API.md`

The plugin registration pattern is described in ADR #97. (Paid Team/Enterprise
routes follow the same `register(router)` seam in the private repo.)

### Modifying Step Functions

1. Edit template in `sfn_templates/<helper>/sfn.tpl.json`
2. Run graph validation: `pytest tests/sdk/test_sfn_flow.py -v`
3. Run JSONata tests: `cd tests/sfn_jsonata && npm test`
4. Run smoke tests: `pytest tests/sdk/test_smoke.py -v`
5. Update ASL snapshots if needed: `SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots.py`
6. Deploy to dev: `sam deploy`
7. Test in AWS Console

---

## Sign your commits (GPG)

Every commit must carry a GPG signature. CI enforces this — unsigned commits
will be rejected on merge.

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
- [ ] Constants synced (`make sync-constants`)
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
