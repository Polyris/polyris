"""
Local execution and validation for polyris pipelines.

Usage:
    from polyris.local import validate, dry_run, run

CLI (installed as polyris-local):
    polyris-local                         # Dry run — preview execution plan
    polyris-local --validate              # Validate structure + exit 0/1
    polyris-local --mock                  # Simulate execution with mock results
    polyris-local --dry-run --asl         # Include generated ASL in output
    polyris-local --file other.py         # Use a different pipeline file

All commands read dag.py in the current directory by default.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Callable, cast
from dataclasses import dataclass, field

from .dag import DAG
from .task import Task
from .generators import generate_step_function_json, generate_mermaid
from .validation import validate_asl_from_dag
from ._ansi import c, BOLD, BOLD_GREEN, CYAN, RED, YELLOW


# ═══════════════════════════════════════════════════════════════
# Data Classes
# ═══════════════════════════════════════════════════════════════

@dataclass
class TaskResult:
    """Result of a task execution."""
    task_id: str
    status: str  # success, failed, skipped
    start_time: datetime
    end_time: datetime
    output: Any = None
    error: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        return int((self.end_time - self.start_time).total_seconds() * 1000)


@dataclass
class ExecutionResult:
    """Result of a DAG execution."""
    dag_id: str
    status: str  # success, failed, partial
    start_time: datetime
    end_time: datetime
    task_results: List[TaskResult] = field(default_factory=list)

    @property
    def duration_ms(self) -> int:
        return int((self.end_time - self.start_time).total_seconds() * 1000)

    def summary(self) -> str:
        success = sum(1 for t in self.task_results if t.status == 'success')
        failed = sum(1 for t in self.task_results if t.status == 'failed')
        skipped = sum(1 for t in self.task_results if t.status == 'skipped')
        return f"✅ {success} succeeded, ❌ {failed} failed, ⏭️ {skipped} skipped"


# ═══════════════════════════════════════════════════════════════
# Validation
# ═══════════════════════════════════════════════════════════════

def validate(dag: DAG, verbose: bool = True) -> Dict[str, Any]:
    """
    Validate DAG structure and generated ASL.

    Delegates to validate_asl_from_dag for full checks: placeholder ARNs,
    role errors, trigger_rule errors, and ASL structural validity.

    Returns:
        {'valid': bool, 'errors': [...], 'warnings': [...], 'info': {...}}
    """
    if verbose:
        print(f"🔍 {c('Validating:', BOLD)} {c(dag.dag_id, CYAN)} ({len(dag.tasks)} tasks, schedule={dag.schedule})")
    # Detect cycles early — generate_step_function_json (called by validate_asl_from_dag)
    # never calls topological_sort(), so cyclic DAGs would otherwise pass with valid=True.
    try:
        dag.topological_sort()
    except Exception as e:
        return {
            'valid': False,
            'errors': [str(e)],
            'warnings': [],
            'info': {
                'dag_id': dag.dag_id,
                'task_count': len(dag.tasks),
                'schedule': dag.schedule,
            },
        }
    # Check that all dependency objects are registered in this DAG — a Task wired via >>
    # but created outside `with DAG():` won't appear in dag.tasks and would silently
    # generate a broken SFN template.  Use id() so the check works regardless of whether
    # dependency objects define __hash__ / __eq__.  Include dag.steps so that valid
    # step-based wires (wait_step >> task) don't trigger a false-positive error.
    # Check both task deps and step deps so a foreign TaskInstance wired via
    # `foreign_instance >> step` is also caught.
    registered_ids = {id(t) for t in dag.tasks} | {id(s) for s in getattr(dag, 'steps', [])}
    dep_errors = [
        f"Task '{t.task_id}' depends on '{getattr(dep, 'task_id', None) or getattr(dep, 'node_id', 'unknown')}'"
        f" which is not registered in this DAG"
        for t in dag.tasks
        for dep in t.dependencies
        if id(dep) not in registered_ids
    ] + [
        f"Step '{s.step_id}' depends on '{getattr(dep, 'task_id', None) or getattr(dep, 'node_id', 'unknown')}'"
        f" which is not registered in this DAG"
        for s in getattr(dag, 'steps', [])
        for dep in s.dependencies
        if id(dep) not in registered_ids
    ]
    if dep_errors:
        return {
            'valid': False,
            'errors': dep_errors,
            'warnings': [],
            'info': {
                'dag_id': dag.dag_id,
                'task_count': len(dag.tasks),
                'schedule': dag.schedule,
            },
        }
    is_valid, errors, warnings = validate_asl_from_dag(dag, verbose=verbose)
    return {
        'valid': is_valid,
        'errors': errors,
        'warnings': warnings,
        'info': {
            'dag_id': dag.dag_id,
            'task_count': len(dag.tasks),
            'schedule': dag.schedule,
        },
    }


# ═══════════════════════════════════════════════════════════════
# Dry Run
# ═══════════════════════════════════════════════════════════════

def dry_run(dag: DAG, show_asl: bool = False, show_mermaid: bool = True) -> Dict[str, Any]:
    """
    Show what would happen without actually executing.

    Displays DAG info, execution order, optional ASL + Mermaid, and
    the full validation result so any errors are visible before deploy.
    Returns the validation result dict so callers can use it for exit-code
    decisions without calling validate() a second time.
    """
    sep = c("=" * 60, BOLD)
    print(sep)
    print(f"🚀 {c('DRY RUN:', BOLD)} {c(dag.dag_id, CYAN)}")
    print(sep)
    print()

    # DAG info
    print(c("📋 DAG Configuration:", BOLD))
    print(f"   ID:          {c(dag.dag_id, CYAN)}")
    print(f"   Schedule:    {dag.schedule or 'None (manual)'}")
    print(f"   Description: {dag.description or 'N/A'}")
    print(f"   Tags:        {dag.tags or []}")
    print()

    # Task execution order
    topo_error: Optional[str] = None
    print(c("📝 Execution Order:", BOLD))
    try:
        ordered_tasks = dag.topological_sort()
        for i, task in enumerate(ordered_tasks, 1):
            deps = [d.node_id for d in task.dependencies]
            deps_str = f" (after: {', '.join(deps)})" if deps else ""
            print(f"   {c(str(i) + '.', BOLD)} {c(task.task_id, CYAN)}{deps_str}")
            print(f"      Type:    {task.task_type}")
            if task.arn:
                print(f"      ARN:     {task.arn}")
            if task.trigger_rule != 'all_success':
                print(f"      Trigger: {c(task.trigger_rule, YELLOW)}")
    except Exception as e:
        topo_error = str(e)
        print(f"   {c('❌ Error:', RED)} {topo_error}")
    print()

    # Mermaid diagram
    if show_mermaid:
        print(c("📊 DAG Diagram (Mermaid):", BOLD))
        print("```mermaid")
        print(generate_mermaid(dag))
        print("```")
        print()

    # ASL — skip for cyclic DAGs: generate_step_function_json doesn't call
    # topological_sort, so it would produce misleading output for a broken DAG.
    if show_asl and not topo_error:
        print(c("📜 Generated ASL (Step Functions JSON):", BOLD))
        asl = generate_step_function_json(dag)
        print(json.dumps(json.loads(asl), indent=2))
        print()

    # Validation — cycle was already reported above; skip reprinting those errors.
    print(c("🔍 Validation:", BOLD))
    result = validate(dag, verbose=False)
    if result['valid']:
        print(f"   {c('✅ All checks passed', BOLD_GREEN)}")
    elif not topo_error:
        for err in result['errors']:
            print(f"   {c('❌', RED)} {err}")
    for w in result['warnings']:
        print(f"   {c('⚠️ ', YELLOW)} {w}")
    print()

    print(sep)
    print(c("ℹ️  This is a dry run. No resources were created or modified.", BOLD))
    print(sep)
    return result


# ═══════════════════════════════════════════════════════════════
# Mock Execution
# ═══════════════════════════════════════════════════════════════

def run(
    dag: DAG,
    mock: bool = True,
    localstack: bool = False,
    localstack_endpoint: str = "http://localhost:4566",
    task_mocks: Optional[Dict[str, Any]] = None,
    on_task_start: Optional[Callable[[str], None]] = None,
    on_task_complete: Optional[Callable[["TaskResult"], None]] = None,
) -> "ExecutionResult":
    """
    Run DAG locally.

    Modes:
    - mock=True (default): Simulate execution with mock results.
    - localstack=True: Execute against LocalStack (requires LocalStack running
      at localhost:4566 — exercised by e2e/smoke, not unit tests).
    """
    if localstack:
        return _run_localstack(dag, localstack_endpoint, on_task_start, on_task_complete)  # pragma: no cover — requires LocalStack running at localhost:4566
    else:
        return _run_mock(dag, task_mocks or {}, on_task_start, on_task_complete)


def _run_mock(
    dag: DAG,
    task_mocks: Dict[str, Any],
    on_task_start: Optional[Callable],
    on_task_complete: Optional[Callable],
) -> ExecutionResult:
    """Run with mock execution."""

    sep = c("=" * 60, BOLD)
    print(sep)
    print(f"🧪 {c('MOCK EXECUTION:', BOLD)} {c(dag.dag_id, CYAN)}")
    print(sep)
    print()

    start_time = datetime.now(timezone.utc)
    task_results: List[Any] = []
    task_outputs = {}  # Store outputs for downstream tasks

    try:
        ordered_tasks = dag.topological_sort()
    except Exception as e:
        print(f"{c('❌ Failed to sort tasks:', RED)} {e}")
        return ExecutionResult(
            dag_id=dag.dag_id,
            status='failed',
            start_time=start_time,
            end_time=datetime.now(timezone.utc),
        )

    for task in ordered_tasks:
        task_start = datetime.now(timezone.utc)

        if on_task_start:
            on_task_start(task.task_id)

        print(f"▶️  {c('Running:', BOLD)} {c(task.task_id, CYAN)}")

        # Check trigger rule
        should_run, reason = _evaluate_trigger_rule(task, task_results)

        if not should_run:
            print(f"   {c('⏭️  Skipped:', YELLOW)} {reason}")
            result = TaskResult(
                task_id=task.task_id,
                status='skipped',
                start_time=task_start,
                end_time=datetime.now(timezone.utc),
            )
            task_results.append(result)
            if on_task_complete:
                on_task_complete(result)
            continue

        # Simulate execution
        try:
            # Get mock output or generate default
            if task.task_id in task_mocks:
                output = task_mocks[task.task_id]
            elif task.python_callable:
                output = task.python_callable()
            else:  # pragma: no cover — every DSL @task.* decorator sets python_callable to the decorated function (never None)
                output = {'status': 'completed', 'task_id': task.task_id}

            # Simulate delay
            time.sleep(0.1)

            task_outputs[task.task_id] = output

            result = TaskResult(
                task_id=task.task_id,
                status='success',
                start_time=task_start,
                end_time=datetime.now(timezone.utc),
                output=output,
            )
            print(f"   {c('✅ Success', BOLD_GREEN)} ({result.duration_ms}ms)")

        except Exception as e:
            result = TaskResult(
                task_id=task.task_id,
                status='failed',
                start_time=task_start,
                end_time=datetime.now(timezone.utc),
                error=str(e),
            )
            print(f"   {c('❌ Failed:', RED)} {e}")

        task_results.append(result)

        if on_task_complete:
            on_task_complete(result)

    end_time = datetime.now(timezone.utc)

    # Determine overall status
    failed_count = sum(1 for r in task_results if r.status == 'failed')
    if failed_count == 0:
        status = 'success'
    elif failed_count == len(task_results):
        status = 'failed'
    else:
        status = 'partial'

    execution_result = ExecutionResult(
        dag_id=dag.dag_id,
        status=status,
        start_time=start_time,
        end_time=end_time,
        task_results=task_results,
    )

    print()
    print(sep)
    print(f"📊 {c('Result:', BOLD)} {execution_result.summary()}")
    print(f"⏱️  {c('Duration:', BOLD)} {execution_result.duration_ms}ms")
    print(sep)

    return execution_result


def _run_localstack(  # pragma: no cover — requires LocalStack running at localhost:4566; exercised by e2e/smoke
    dag: DAG,
    endpoint: str,
    on_task_start: Optional[Callable],
    on_task_complete: Optional[Callable],
) -> ExecutionResult:
    """Run with LocalStack."""

    print("=" * 60)
    print(f"🐳 LOCALSTACK EXECUTION: {dag.dag_id}")
    print(f"   Endpoint: {endpoint}")
    print("=" * 60)
    print()

    try:
        import boto3
        from botocore.exceptions import ClientError, BotoCoreError
    except ImportError:
        print("❌ boto3 is required for LocalStack execution")
        print("   pip install boto3")
        raise

    start_time = datetime.now(timezone.utc)

    # Create LocalStack clients
    sfn = boto3.client(
        'stepfunctions',
        endpoint_url=endpoint,
        region_name='us-east-1',
        aws_access_key_id='test',
        aws_secret_access_key='test',
    )

    # Generate and deploy state machine
    asl_json = generate_step_function_json(dag)

    try:
        # Create or update state machine
        sm_name = f"local-{dag.dag_id}"

        # Try to delete existing
        try:
            existing = sfn.list_state_machines()
            for sm in existing['stateMachines']:
                if sm['name'] == sm_name:
                    sfn.delete_state_machine(stateMachineArn=sm['stateMachineArn'])
                    time.sleep(0.5)
        except (ClientError, BotoCoreError) as e:
            # SM might not exist yet, or LocalStack glitch — proceed to create
            print(f"[local] Skipped deleting old state machine: {e}")

        # Create new
        response = sfn.create_state_machine(
            name=sm_name,
            definition=asl_json,
            roleArn='arn:aws:iam::000000000000:role/test-role',
        )
        sm_arn = response['stateMachineArn']
        print(f"✓ Created state machine: {sm_name}")

        # Start execution with deterministic name
        exec_name = f"{sm_name}-local-{start_time.strftime('%Y%m%d-%H%M%S')}"
        exec_response = sfn.start_execution(
            stateMachineArn=sm_arn,
            name=exec_name,
            input=json.dumps({'execution_date': start_time.isoformat()})
        )
        exec_arn = exec_response['executionArn']
        print(f"✓ Started execution: {exec_arn}")

        # Poll for completion
        while True:
            status_response = sfn.describe_execution(executionArn=exec_arn)
            status = status_response['status']

            if status in ('SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED'):
                break

            print(f"   ⏳ Status: {status}")
            time.sleep(1)

        end_time = datetime.now(timezone.utc)

        # Get execution history for task results
        history = sfn.get_execution_history(executionArn=exec_arn)
        task_results = _parse_execution_history(cast(List[Dict[str, Any]], history['events']))

        print()
        print(f"✓ Execution completed: {status}")

        return ExecutionResult(
            dag_id=dag.dag_id,
            status='success' if status == 'SUCCEEDED' else 'failed',
            start_time=start_time,
            end_time=end_time,
            task_results=task_results,
        )

    except Exception as e:
        print(f"❌ LocalStack execution failed: {e}")
        return ExecutionResult(
            dag_id=dag.dag_id,
            status='failed',
            start_time=start_time,
            end_time=datetime.now(timezone.utc),
        )


def _evaluate_trigger_rule(task: Task, previous_results: List[TaskResult]) -> tuple:
    """Evaluate if task should run based on trigger rule.

    Mirrors the 11 rules evaluate_deps implements for the real deployed
    pipeline (docs/features/DSL.md#trigger-rules), adapted to this mock
    runner's synchronous execution model: `run()` processes tasks in
    topological order, so every dependency of `task` has already produced a
    terminal TaskResult (success/failed/skipped) by the time this function is
    called — there is no 'pending'/'wait' case here, unlike evaluate_deps'
    async DDB-polling model. This mock also has no skip_origin concept (no
    manual-vs-rule distinction — see ADR #115): every skip here comes from a
    trigger_rule not being satisfied, so `all_success` correctly treats ANY
    skipped upstream as blocking (no cascade nuance needed).
    """

    # Get upstream task results
    upstream_ids = {t.node_id for t in task.dependencies}
    upstream_results = [r for r in previous_results if r.task_id in upstream_ids]

    if not upstream_ids:
        return True, "no upstream dependencies"

    success_count = sum(1 for r in upstream_results if r.status == 'success')
    skipped_count = sum(1 for r in upstream_results if r.status == 'skipped')
    done_count = len(upstream_results)  # always == total in this synchronous model
    total = len(upstream_ids)

    rule = task.trigger_rule

    if rule == 'all_success':
        if success_count == total:
            return True, "all_success"
        return False, f"not all succeeded ({success_count}/{total})"

    elif rule == 'all_done':
        if done_count == total:
            return True, "all_done"
        return False, f"not all done ({done_count}/{total})"

    elif rule == 'all_skipped':
        if done_count == total and skipped_count == total:
            return True, "all_skipped"
        return False, (f"not all done ({done_count}/{total})" if done_count < total
                        else f"not all skipped ({skipped_count}/{total})")

    elif rule == 'one_success':
        if success_count >= 1:
            return True, "one_success"
        return False, "no success yet"

    elif rule == 'none_skipped':
        if skipped_count == 0:
            return True, "none_skipped"
        return False, f"{skipped_count} skipped"

    # Unknown rule name (including any of ADR #117's 6 removed names): default
    # to all_success's semantics, matching evaluate_deps' fallback for
    # consistency (ADR #115) rather than silently letting an unrecognized
    # rule always run. validate_asl_from_dag catches removed/unknown rules
    # earlier with a specific message — this is a defensive fallback, not
    # the primary way a user finds out a rule name isn't supported.
    if success_count == total:
        return True, "unknown_rule_defaulted_to_all_success"
    return False, f"unknown trigger_rule {rule!r}; defaulted to all_success, not all succeeded ({success_count}/{total})"


def _parse_execution_history(events: List[Dict]) -> List[TaskResult]:
    """Parse SFN execution history into TaskResults."""
    results = []
    task_starts = {}

    for idx, event in enumerate(events):
        event_type = event['type']
        timestamp = event['timestamp']

        if 'TaskStateEntered' in event_type:
            details = event.get('stateEnteredEventDetails', {})
            task_id = details.get('name', 'unknown')
            task_starts[task_id] = timestamp

        elif 'TaskStateExited' in event_type:
            details = event.get('stateExitedEventDetails', {})
            task_id = details.get('name', 'unknown')
            output = json.loads(details.get('output', '{}'))

            results.append(TaskResult(
                task_id=task_id,
                status='success',
                start_time=task_starts.get(task_id, timestamp),
                end_time=timestamp,
                output=output,
            ))

        elif 'TaskFailed' in event_type or 'ExecutionFailed' in event_type:
            details = event.get('taskFailedEventDetails', event.get('executionFailedEventDetails', {}))
            error = details.get('error', 'Unknown')
            cause = details.get('cause', '')

            # Try to find which task failed
            task_id = 'unknown'
            for e in reversed(events[:idx]):
                if 'TaskStateEntered' in e.get('type', ''):
                    task_id = e.get('stateEnteredEventDetails', {}).get('name', 'unknown')
                    break

            results.append(TaskResult(
                task_id=task_id,
                status='failed',
                start_time=task_starts.get(task_id, timestamp),
                end_time=timestamp,
                error=f"{error}: {cause}",
            ))

    return results


# ═══════════════════════════════════════════════════════════════
# CLI Integration
# ═══════════════════════════════════════════════════════════════

def _load_dag_from_file(dag_file: str) -> DAG:
    """Load a DAG from a pipeline file using DAG.__exit__ instrumentation.

    Uses the same discovery pattern as extract_dag_info / _validate_single
    (CLAUDE.md §): works for `with DAG("name"):` blocks that don't assign
    `as dag` — unlike vars(mod) which only finds module-level bindings.

    Only DAGs defined in the pipeline file itself are returned — DAGs from
    imported modules are excluded via a sys.modules locality filter (CLAUDE.md).
    """
    import importlib.util

    created_dags: List[DAG] = []
    original_exit = DAG.__exit__

    def tracking_exit(self: Any, *args: Any) -> Any:
        created_dags.append(self)
        return original_exit(self, *args)

    DAG.__exit__ = tracking_exit  # type: ignore[method-assign]
    try:
        spec = importlib.util.spec_from_file_location("dag_module", dag_file)
        if spec is None or spec.loader is None:  # pragma: no cover — defensive: spec_from_file_location returns None only for paths Python cannot load
            raise ImportError(f"Cannot load module from {dag_file}")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as e:
            if isinstance(e, SystemExit) and e.code in (0, None):
                pass  # exit(0) or bare sys.exit() (code=None) means ran to completion
            else:
                code = e.code if isinstance(e, SystemExit) else None
                msg = f"sys.exit({code})" if code is not None else str(e)
                print(f"❌ Error loading {c(dag_file, BOLD)}: {c(msg, RED)}")
                sys.exit(1)
    finally:
        DAG.__exit__ = original_exit  # type: ignore[method-assign]

    # Locality filter (CLAUDE.md): exclude DAGs that appear as named attributes in any
    # imported module — those were defined elsewhere and merely imported by the pipeline
    # file. The pipeline file's own module is not in sys.modules (we use module_from_spec
    # without adding it), so any DAG found in sys.modules came from an import.
    imported_dag_ids: set[int] = set()
    for _mod in sys.modules.values():
        try:
            for _obj in vars(_mod).values():
                if isinstance(_obj, DAG):
                    imported_dag_ids.add(id(_obj))
        except TypeError:  # pragma: no cover — built-in modules without __dict__
            pass

    local_dags = [d for d in created_dags if id(d) not in imported_dag_ids]

    if not local_dags:
        print(f"❌ No DAG found in {c(dag_file, BOLD)}")
        sys.exit(1)

    return local_dags[-1]


def run_cli(dag_file: str, mode: str = 'dry-run', **kwargs: Any) -> None:
    """
    Run from CLI.

    Args:
        dag_file: Path to Python file containing DAG
        mode: One of 'dry-run', 'validate', 'mock', 'localstack'
    """
    dag = _load_dag_from_file(dag_file)

    if mode == 'dry-run':
        dry_run(dag, **kwargs)
    elif mode == 'validate':
        validate(dag, **kwargs)
    elif mode == 'mock':
        run(dag, mock=True, **kwargs)
    elif mode == 'localstack':
        run(dag, localstack=True, **kwargs)  # pragma: no cover — requires LocalStack running at localhost:4566
    else:
        print(f"❌ Unknown mode: {mode}")


def main() -> None:
    """CLI entry point for polyris-local.

    Usage:
        polyris-local                    # Dry run (default)
        polyris-local --validate         # Validate and exit 0/1
        polyris-local --mock             # Simulate execution
        polyris-local --file other.py    # Use a different pipeline file
        polyris-local --dry-run --asl    # Include ASL JSON in output
    """
    parser = argparse.ArgumentParser(
        prog='polyris-local',
        description='Validate and preview polyris pipelines without deploying to AWS.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  polyris-local                    # Dry run (shows execution plan)
  polyris-local --validate         # Validate and exit 0/1
  polyris-local --mock             # Simulate execution with mock results
  polyris-local --file other.py    # Use a different pipeline file
  polyris-local --dry-run --asl    # Include generated ASL JSON""",
    )
    parser.add_argument(
        '--file', '-f',
        default='dag.py',
        metavar='DAG_FILE',
        help='pipeline file to load (default: dag.py)',
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        '--validate',
        action='store_true',
        help='validate pipeline structure and exit 0 (valid) or 1 (invalid)',
    )
    mode_group.add_argument(
        '--mock',
        action='store_true',
        help='simulate execution with mock results',
    )
    mode_group.add_argument(
        '--localstack',
        action='store_true',
        help='execute against LocalStack (requires LocalStack running at localhost:4566)',
    )
    mode_group.add_argument(
        '--dry-run',
        dest='dry_run',
        action='store_true',
        help='show execution plan without running (default when no mode given)',
    )
    parser.add_argument(
        '--asl',
        action='store_true',
        help='include generated Step Functions ASL JSON in --dry-run output',
    )

    args = parser.parse_args()

    dag = _load_dag_from_file(args.file)

    if args.validate:
        sys.exit(0 if validate(dag)['valid'] else 1)
    elif args.mock:
        sys.exit(0 if run(dag, mock=True).status == 'success' else 1)
    elif args.localstack:  # pragma: no cover — requires LocalStack running at localhost:4566
        sys.exit(0 if run(dag, localstack=True).status == 'success' else 1)
    else:
        # Default: --dry-run (also reached when --dry-run is passed explicitly)
        sys.exit(0 if dry_run(dag, show_asl=args.asl)['valid'] else 1)


if __name__ == '__main__':  # pragma: no cover — module entrypoint
    main()
