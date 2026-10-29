# Local Testing

Test polyris pipelines locally without deploying to AWS.

## Quick start

```bash
cd my-pipeline/

polyris-local             # dry run — execution plan + validation
polyris-local --validate  # exit 0 if valid, 1 if not
polyris-local --mock      # simulate execution with mock results
```

All commands read `dag.py` in the current directory. Use `--file other.py` to target a different file.

---

## Commands

### Dry run (default)

Shows DAG configuration, execution order, and validation result. Nothing is created or modified.

```bash
polyris-local
polyris-local --dry-run --asl   # also print generated Step Functions JSON
```

### Validate

Checks for placeholder ARNs, cycle detection, trigger rule errors, and ASL structural validity.
Exits 0 if valid, 1 if not — suitable for CI.

```bash
polyris-local --validate
```

### Mock execution

Simulates the pipeline locally by calling each task's Python function in topological order.
Respects trigger rules (`all_success`, `all_done`, etc.).

```bash
polyris-local --mock
```

---

## Python API

The same functions are available for use in pipeline files or tests:

```python
from polyris.local import validate, dry_run, run

result = validate(dag, verbose=False)
# {'valid': True, 'errors': [], 'warnings': [], 'info': {'dag_id': ..., 'task_count': 2, 'schedule': '@daily'}}

result = dry_run(dag, show_asl=False, show_mermaid=True)
# returns the same dict as validate()

result = run(dag, mock=True)
result = run(dag, mock=True, task_mocks={'extract': {'records': 100}})
result = run(dag, localstack=True)  # requires LocalStack at localhost:4566
```

### `ExecutionResult`

```python
result = run(dag, mock=True)

result.dag_id        # 'my-pipeline'
result.status        # 'success', 'failed', or 'partial'
result.duration_ms   # 210
result.task_results  # List[TaskResult]
result.summary()     # '✅ 2 succeeded, ❌ 0 failed, ⏭️ 0 skipped'

for t in result.task_results:
    print(f"{t.task_id}: {t.status} ({t.duration_ms}ms)")
```

---

## Testing pattern

```python
# tests/test_my_pipeline.py
from polyris.local import validate, run

def test_valid():
    result = validate(dag, verbose=False)
    assert result['valid'] is True

def test_mock():
    result = run(dag, mock=True)
    assert result.status == 'success'
```

---

## Trigger rule note

`run(dag, mock=True)` is a plain Python loop — it does not reproduce the deployed pipeline's
`Parallel`-based execution or its pause-on-failure behaviour. A green mock run does not guarantee
identical behaviour in production. See `docs/features/DSL.md#trigger-rules` for what each
trigger rule does in a live deployment.
