# Development Scripts

Utility commands for maintaining and validating the polyris codebase.

---

## Lambda Constants Sync

Lambda functions cannot share code directly, so constants must be duplicated across Lambdas.

### Source of Truth

```
sam/lambdas/_shared/constants.py
```

### Keeping in Sync

When you modify `_shared/constants.py`, copy it to the Lambda directories:

```bash
cp sam/lambdas/_shared/constants.py sam/lambdas/evaluate_deps/constants.py
```

### Validation

```bash
# Check if files are in sync
make sync-constants

# Or manually:
diff sam/lambdas/_shared/constants.py sam/lambdas/evaluate_deps/constants.py
```

### Adding New Statuses

1. Add to `_shared/constants.py` (source of truth)
2. Copy to `evaluate_deps/constants.py`
3. Update `console_api/constants.py` if needed (has extended fields)
4. Run `make sync-constants` to verify

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
make check             # Runs: lint + sync-constants + smoke-pipelines + test
```

### Python SDK Tests

```bash
# All SDK tests (smoke, snapshots, flow, templates, trigger rules, api routes)
pytest tests/ -v --ignore=tests/integration/

# Specific test files
pytest tests/sdk/test_smoke.py -v              # Core: DAG structure, ASL generation
pytest tests/sdk/test_asl_snapshots.py -v      # Golden file comparison of generated JSON
pytest tests/sdk/test_sfn_flow.py -v           # SFN template graph validation (13 templates)
pytest tests/sdk/test_templates.py -v          # orchestration_timeout, route table, resilience
pytest tests/backend/test_api_routes.py -v         # All 27 free API routes registered
pytest tests/sdk/test_trigger_rules.py -v      # 5 trigger rules Python ↔ JSONata sync

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
# evaluate_deps (trigger rule evaluation, BatchGetItem)
cd sam/lambdas/evaluate_deps && PYTHONPATH=. pytest test_evaluate_deps.py -v

# check_assets (asset freshness, wait_for logic)
cd sam/lambdas/check_assets && PYTHONPATH=. pytest test_check_assets.py -v

# notify_asset_subscribers (event publishing, subscriber notification)
cd sam/lambdas/notify_asset_subscribers && PYTHONPATH=. pytest test_notify_asset_subscribers.py -v

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
| **Lambda Tests** | Syntax check, constants sync, evaluate_deps, check_assets, notify_asset_subscribers, console_api |
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
