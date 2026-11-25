# Local Testing

Check and simulate a pipeline on your machine, without AWS: `polyris-local` from the shell, `polyris.local` from your tests.

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

Runs the pipeline's tasks in dependency order, each by calling its decorated Python function, and applies the trigger rules (`all_success`, `all_done`, …). No AWS service is called. Exits 0 if every task succeeds.

```bash
polyris-local --mock
```

---

## Python API

The same functions, for your tests. Install polyris into your test environment (the CLI's own environment is separate):

```bash
pip install "polyris @ https://github.com/Polyris/polyris/archive/refs/tags/v1.3.0.tar.gz"
```

Then, from the pipeline folder:

```python
from polyris.local import validate, dry_run, run
from dag import dag          # dag.py in this folder

result = validate(dag, verbose=False)
# {'valid': True, 'errors': [], 'warnings': [], 'info': {'dag_id': ..., 'task_count': 2, 'schedule': '@daily'}}

result = dry_run(dag, show_asl=False, show_mermaid=True)
# returns the same dict as validate()

result = run(dag, mock=True)
result = run(dag, mock=True, task_mocks={'extract': {'records': 100}})
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
# test_my_pipeline.py, next to dag.py
from polyris.local import validate, run
from dag import dag

def test_valid():
    result = validate(dag, verbose=False)
    assert result['valid'] is True

def test_mock():
    result = run(dag, mock=True)
    assert result.status == 'success'
```

---

## Trigger rule note

`run(dag, mock=True)` evaluates each trigger rule with the same code as a deployed run, so it records `skipped` or `upstream_failed` where a deployed run does, and fails a task whose rule it does not know. It is still a plain Python loop: a failed task does not retry or wait for a decision, and every skip in it counts as a rule's skip. A green mock run does not guarantee the same behaviour in AWS. What each trigger rule does: [DSL.md → Trigger rules](../features/DSL.md#trigger-rules).
