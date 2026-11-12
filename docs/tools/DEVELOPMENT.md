# Development Scripts

Utility commands for maintaining and validating the polyris codebase.

---

## API Input Validation

The Console API includes validation helpers in `utils.py`:

```python
from utils import validate_date, validate_execution_name, validate_pipeline_name

# Date validation (YYYY-MM-DD format)
valid, error = validate_date('2025-01-15')  # (True, None)
valid, error = validate_date('invalid')     # (False, 'Date must be in YYYY-MM-DD format')

# Execution name validation
valid, error = validate_execution_name('task-2025-01-15-abc123')

# Pipeline name validation
valid, error = validate_pipeline_name('my-pipeline')
```

All backfill endpoints validate date inputs before processing:

```python
# backfill.py
valid, error = validate_date(start_date)
if not valid:
    return cors_response(400, {'error': f'start_date: {error}'})
```

---

## Running Tests

### All Tests

```bash
make test              # Runs: test-sdk + test-lambdas + test-sfn-jsonata + test-ui
make check             # Runs: lint + generated-code drift + typecheck + smoke-pipelines + test
```

### Python SDK Tests

```bash
# All SDK tests (smoke, snapshots, flow, templates, trigger rules, api routes)
pytest tests/ -v --ignore=tests/integration/

# Specific test files
pytest tests/sdk/test_smoke.py -v              # Core: DAG structure, ASL generation
pytest tests/sdk/test_asl_snapshots.py -v      # Golden file comparison of generated JSON
pytest tests/sdk/test_sfn_flow.py -v           # SFN template graph validation (every orchestration template)
pytest tests/sdk/test_templates.py -v          # orchestration_timeout, route table, resilience
pytest tests/backend/test_api_routes.py -v         # All 28 free API routes registered
pytest tests/sdk/test_coordination_trigger_rules.py -v  # The 5 trigger rules (polyris/coordination)

# Update ASL snapshots after intentional changes
SNAPSHOT_UPDATE=1 pytest tests/sdk/test_asl_snapshots.py
```

### SFN JSONata Expression Tests

Tests critical JSONata expressions from SFN templates with mock inputs.
Catches: broken defaults, type conversion bugs, conditional logic errors.

```bash
cd tests/sfn_jsonata
npm install    # first time only
npm test       # 34 tests + 550 expression compilation smoke test
```

### Lambda Unit Tests

```bash
# coordination (reconcile, asset-fanout; moto tables built from sam/template.yaml)
cd sam/lambdas/coordination && PYTHONPATH=. pytest tests/ -v

# console_api (API routes, pagination, validation)
cd sam/lambdas/console_api && PYTHONPATH=. pytest tests/ -v
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
| **Python 3.11/3.12** | ruff lint, mypy, smoke tests, `pytest tests/`, trigger rules, pipeline imports |
| **Lambda Tests** | Syntax check, logger sync, every Lambda suite (console_api, coordination, query_subscriptions, …) |
| **SFN Templates** | ASL JSON validation, `test_sfn_flow.py` (graph integrity), JSONata expression tests |
| **SAM** | `cfn-lint sam/template.yaml` |
| **UI** | npm lint, typecheck, `vitest run`, `next build` |

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
