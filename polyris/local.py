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
from typing import Dict, Any, Optional, List, Callable, Tuple
from dataclasses import dataclass, field

from contextlib import ExitStack

from .constants import SkipOrigin
from .coordination import trigger_rules
from .dag import DAG
from .loader import PipelineLoadError, load_pipeline, open_pipeline
from .task import Task
from .generators import generate_step_function_json, generate_mermaid
from .validation import validate_asl_from_dag
from ._ansi import c, colorize_parser, BOLD, BOLD_GREEN, CYAN, RED, YELLOW


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
        blocked = sum(1 for t in self.task_results if t.status == 'upstream_failed')
        return (f"✅ {success} succeeded, ❌ {failed} failed, ⏭️ {skipped} skipped"
                + (f", ⛔ {blocked} upstream_failed" if blocked else ""))


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
    print(f"   Schedule:    {c(str(dag.schedule), CYAN) if dag.schedule else 'None (manual)'}")
    print(f"   Description: {c(dag.description, CYAN) if dag.description else 'N/A'}")
    print(f"   Tags:        {c(str(dag.tags), CYAN) if dag.tags else []}")
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
            print(f"      Type:    {c(task.task_type, CYAN)}")
            if task.arn:
                print(f"      ARN:     {c(task.arn, CYAN)}")
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
    *,
    mock: bool = True,
    task_mocks: Optional[Dict[str, Any]] = None,
    on_task_start: Optional[Callable[[str], None]] = None,
    on_task_complete: Optional[Callable[["TaskResult"], None]] = None,
) -> "ExecutionResult":
    """Run the DAG locally, each task with a mock result (``task_mocks``, or a success).

    A mock run is the only local run: a run of 1.3 needs the platform's engine, so the
    LocalStack mode, which deployed the pipeline's state machine alone, could run no task and
    is gone. ``mock`` stays for the calls that pass it; ``mock=False`` is refused rather than
    quietly mocked.
    """
    if not mock:
        raise ValueError("polyris.local.run() gives every task a mock result; to run the pipeline "
                         "for real, deploy it with polyris-deploy and start it in the console.")
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
        verdict, reason = _evaluate_trigger_rule(task, task_results)

        if verdict != trigger_rules.READY:
            not_run = _NOT_RUN[verdict]
            print(f"   {c(f'⏭️  {not_run}:', RED if not_run == 'failed' else YELLOW)} {reason}")
            result = TaskResult(
                task_id=task.task_id,
                status=not_run,
                start_time=task_start,
                end_time=datetime.now(timezone.utc),
                error=reason if not_run == 'failed' else None,
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


def _evaluate_trigger_rule(task: Task, previous_results: List[TaskResult]) -> Tuple[str, str]:
    """The verdict of ``task``'s trigger rule over its upstreams' results, and why.

    The engine's own function decides (``trigger_rules.evaluate``, ADR #138
    §6), so a mock run records ``skipped`` or ``upstream_failed`` where a
    deployed run does. ``run()`` takes tasks in topological order, so every
    upstream task already has a final result; every skip here comes from a
    rule, since this runner has no operator and no ``skip_tasks``. An upstream
    with no result, a step this runner does not execute, counts as pending.
    """
    results = {r.task_id: r for r in previous_results}
    statuses = [results[d.node_id].status if d.node_id in results else "pending" for d in task.dependencies]
    origins = [SkipOrigin.RULE if status == "skipped" else None for status in statuses]
    evaluation = trigger_rules.evaluate(task.trigger_rule, statuses, origins)
    return evaluation.verdict, evaluation.reason


#: What a mock run records for a task its rule does not run; a pending upstream cannot finish here.
_NOT_RUN = {trigger_rules.SKIP: "skipped", trigger_rules.UPSTREAM_FAILED: "upstream_failed",
            trigger_rules.FAIL: "failed", trigger_rules.WAIT: "skipped"}


# ═══════════════════════════════════════════════════════════════
# CLI Integration
# ═══════════════════════════════════════════════════════════════

def _load_dag_from_file(dag_file: str, stack: Optional[ExitStack] = None) -> DAG:
    """The DAG a pipeline file makes last, through the one loader every CLI uses (ADR #139).

    With ``stack``, the file stays open (``open_pipeline``) until the stack closes, so a task
    body run after the load can still import a sibling module.
    """
    try:
        loaded = stack.enter_context(open_pipeline(dag_file)) if stack else load_pipeline(dag_file)
    except PipelineLoadError as e:
        print(f"❌ Error loading {c(dag_file, BOLD)}: {c(str(e), RED)}")
        sys.exit(1)
    if not loaded.dags:
        print(f"❌ No DAG found in {c(dag_file, BOLD)}")
        sys.exit(1)
    return loaded.dags[-1]


def run_cli(dag_file: str, mode: str = 'dry-run', **kwargs: Any) -> None:
    """
    Run from CLI.

    Args:
        dag_file: Path to Python file containing DAG
        mode: One of 'dry-run', 'validate', 'mock'
    """
    with ExitStack() as stack:
        dag = _load_dag_from_file(dag_file, stack)

        if mode == 'dry-run':
            dry_run(dag, **kwargs)
        elif mode == 'validate':
            validate(dag, **kwargs)
        elif mode == 'mock':
            run(dag, mock=True, **kwargs)
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

    colorize_parser(parser)
    args = parser.parse_args()

    with ExitStack() as stack:
        dag = _load_dag_from_file(args.file, stack)

        if args.validate:
            sys.exit(0 if validate(dag)['valid'] else 1)
        elif args.mock:
            sys.exit(0 if run(dag, mock=True).status == 'success' else 1)
        else:
            # Default: --dry-run (also reached when --dry-run is passed explicitly)
            sys.exit(0 if dry_run(dag, show_asl=args.asl)['valid'] else 1)


if __name__ == '__main__':  # pragma: no cover — module entrypoint
    main()
