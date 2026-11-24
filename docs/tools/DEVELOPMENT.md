# Development Scripts

Utility commands for maintaining and validating the polyris codebase.

---

## API Input Validation

The Console API validates a date from a request with `validate_date` in `utils.py`:

```python
from utils import validate_date

valid, error = validate_date('2025-01-15')  # (True, None)
valid, error = validate_date('invalid')     # (False, 'Date must be in YYYY-MM-DD format')
```

---

## Running Tests

### All Tests

```bash
make test              # Runs: test-sdk + test-backend + test-integration + test-docs + test-lambdas + test-sfn-jsonata + test-ui
make check             # Runs: lint + generated-code drift + typecheck + smoke-pipelines + test
```

### Python SDK Tests

```bash
# SDK, backend and integration tests (smoke, snapshots, flow, templates, trigger rules, api routes)
python -m pytest tests/sdk tests/backend tests/integration -v

# Docs drift tests: their own session, since next to tests/backend `tests` resolves to console_api's test package
make test-docs

# Specific test files
pytest tests/sdk/test_smoke.py -v              # Core: DAG structure, ASL generation
pytest tests/sdk/test_asl_snapshots.py -v      # Golden file comparison of generated JSON
pytest tests/sdk/test_sfn_flow.py -v           # SFN template graph validation (every orchestration template)
pytest tests/sdk/test_templates.py -v          # orchestration_timeout, route table, resilience
pytest tests/backend/test_api_routes.py -v         # All 24 free API routes registered
pytest tests/sdk/test_coordination_trigger_rules.py -v  # The nine trigger rules and their truth table (polyris/coordination)

# Update ASL snapshots after intentional changes
SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots.py
```

### SFN JSONata Expression Tests

Tests critical JSONata expressions from SFN templates with mock inputs.
Catches: broken defaults, type conversion bugs, conditional logic errors.

```bash
cd tests/sfn_jsonata
npm install    # first time only
npm test       # expression tests, then a compile check of every expression in the templates
```

### Lambda Unit Tests

```bash
# Every Lambda suite, with no AWS profile, region or credentials, as CI runs them
make test-lambdas

# One suite: the Lambda's folder and the checkout's root on PYTHONPATH
cd sam/lambdas/coordination && PYTHONPATH=.:../../.. python3 -m pytest tests/ -v   # dispatcher, start-run, asset-fanout, sweeper
cd sam/lambdas/console_api && PYTHONPATH=.:../../.. python3 -m pytest tests/ -v    # API routes, pagination, validation
```

### UI Tests

```bash
cd ui
npm test          # Watch mode
npm run test:run  # Single run (CI)
```

---

## CI Pipeline

All tests run automatically on push/PR to main via GitHub Actions (`.github/workflows/ci.yml`).

| Job | What runs |
|-----|-----------|
| **Python 3.11/3.12** | ruff lint, mypy, SDK, backend, integration and docs tests, the coverage floor, generated-code drift, the open-core guard, pipeline imports |
| **Lambda Tests** | Syntax check, logger sync, every Lambda suite (console_api, coordination, bucket_cleanup, …) |
| **SFN Templates** | ASL JSON validation, `test_sfn_flow.py` (graph integrity), JSONata expression tests |
| **SAM** | `cfn-lint sam/template.yaml` |
| **UI** | npm lint, typecheck, `vitest run`, `next build`, the open-core build guard |
| **Platform archive** | `sam build` and the release archive `polyris-up` deploys |
| **Docs** | link check (lychee) and prose lint (Vale) |

---

## Code Quality Checks

### Python Syntax

```bash
python -m py_compile polyris/*.py
python -m py_compile sam/lambdas/**/*.py
```

### Type Checking (optional)

```bash
mypy polyris/ --ignore-missing-imports
```

### JavaScript Linting (optional)

```bash
cd ui
npx eslint src/
```
