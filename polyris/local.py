"""
Local execution for polyris pipelines.

Run pipelines locally for testing without deploying to AWS.

Usage:
    from polyris import DAG, task
    from polyris.local import run, dry_run, validate
    
    with DAG("my-pipeline", ...) as dag:
        @task.sfn(arn="...")
        def extract(): pass
        extract()
    
    # Option 1: Dry run (show what would happen)
    dry_run(dag)
    
    # Option 2: Validate (check DAG structure)
    validate(dag)
    
    # Option 3: Run with mocks (simulate execution)
    run(dag, mock=True)
    
    # Option 4: Run with LocalStack
    run(dag, localstack=True)

CLI:
    polyris local my_dag.py --dry-run
    polyris local my_dag.py --validate
    polyris local my_dag.py --mock
    polyris local my_dag.py --localstack
"""

import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Callable
from dataclasses import dataclass, field

from .dag import DAG
from .task import Task
from .generators import generate_step_function_json, generate_mermaid, validate_asl


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
    
    Checks:
    - DAG has tasks
    - No circular dependencies
    - All task dependencies exist
    - Generated ASL is valid
    - Alerts are configured (warning if not)
    
    Args:
        dag: The DAG to validate
        verbose: Print results to stdout
    
    Returns:
        {
            'valid': bool,
            'errors': [...],
            'warnings': [...],
            'info': {...}
        }
    """
    errors = []
    warnings = []
    info = {
        'dag_id': dag.dag_id,
        'task_count': len(dag.tasks),
        'schedule': dag.schedule,
    }
    
    if verbose:
        print(f"🔍 Validating DAG: {dag.dag_id}")
        print(f"   Schedule: {dag.schedule or 'None (manual)'}")
        print(f"   Tasks: {len(dag.tasks)}")
        print()
    
    # Check: Has tasks
    if not dag.tasks:
        errors.append("DAG has no tasks")
    
    # Check: Dependencies exist
    task_ids = {t.task_id for t in dag.tasks}
    step_ids = {s.step_id for s in dag.steps} if hasattr(dag, 'steps') else set()
    all_ids = task_ids | step_ids
    for task in dag.tasks:
        for dep in task.dependencies:
            dep_id = getattr(dep, 'task_id', None) or getattr(dep, 'step_id', None)
            if dep_id and dep_id not in all_ids:
                errors.append(f"Task '{task.task_id}' depends on unknown task/step '{dep_id}'")
    
    # Check: No circular dependencies
    try:
        dag.topological_sort()
    except Exception as e:
        errors.append(f"Circular dependency detected: {e}")
    
    # Check: ASL is valid
    try:
        asl_json = generate_step_function_json(dag)
        asl = json.loads(asl_json)
        is_valid, asl_errors, asl_warnings = validate_asl(asl)
        
        errors.extend(asl_errors)
        warnings.extend(asl_warnings)
        
        info['states_count'] = len(asl.get('States', {}))
    except Exception as e:
        errors.append(f"Failed to generate ASL: {e}")
    
    # Check: Task ARNs
    for task in dag.tasks:
        if not task.arn and task.task_type == 'sfn':
            warnings.append(f"Task '{task.task_id}' has no ARN")
    
    is_valid = len(errors) == 0
    
    if verbose:
        if errors:
            print("❌ Errors:")
            for err in errors:
                print(f"   • {err}")
            print()
        
        if warnings:
            print("⚠️  Warnings:")
            for w in warnings:
                print(f"   • {w}")
            print()
        
        if is_valid:
            print("✅ Validation passed!")
        else:
            print("❌ Validation failed!")
    
    return {
        'valid': is_valid,
        'errors': errors,
        'warnings': warnings,
        'info': info
    }


# ═══════════════════════════════════════════════════════════════
# Dry Run
# ═══════════════════════════════════════════════════════════════

def dry_run(dag: DAG, show_asl: bool = False, show_mermaid: bool = True) -> None:
    """
    Show what would happen without actually executing.
    
    Displays:
    - DAG info
    - Task execution order
    - Generated ASL (optional)
    - Mermaid diagram (optional)
    
    Args:
        dag: The DAG to dry-run
        show_asl: Print generated ASL JSON
        show_mermaid: Print Mermaid diagram
    """
    print("=" * 60)
    print(f"🚀 DRY RUN: {dag.dag_id}")
    print("=" * 60)
    print()
    
    # DAG info
    print("📋 DAG Configuration:")
    print(f"   ID: {dag.dag_id}")
    print(f"   Schedule: {dag.schedule or 'None (manual)'}")
    print(f"   Description: {dag.description or 'N/A'}")
    print(f"   Tags: {dag.tags or []}")
    print()
    
    # Task execution order
    print("📝 Execution Order:")
    try:
        ordered_tasks = dag.topological_sort()
        for i, task in enumerate(ordered_tasks, 1):
            deps = [d.node_id for d in task.dependencies]
            deps_str = f" (after: {', '.join(deps)})" if deps else ""
            print(f"   {i}. {task.task_id}{deps_str}")
            print(f"      Type: {task.task_type}")
            if task.arn:
                print(f"      ARN: {task.arn}")
            if task.trigger_rule != 'all_success':
                print(f"      Trigger: {task.trigger_rule}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    print()
    
    # Mermaid diagram
    if show_mermaid:
        print("📊 DAG Diagram (Mermaid):")
        print("```mermaid")
        print(generate_mermaid(dag))
        print("```")
        print()
    
    # ASL
    if show_asl:
        print("📜 Generated ASL (Step Functions JSON):")
        asl = generate_step_function_json(dag)
        print(json.dumps(json.loads(asl), indent=2))
        print()
    
    # Validation
    print("🔍 Validation:")
    result = validate(dag, verbose=False)
    if result['valid']:
        print("   ✅ All checks passed")
    else:
        for err in result['errors']:
            print(f"   ❌ {err}")
    for w in result['warnings']:
        print(f"   ⚠️  {w}")
    print()
    
    print("=" * 60)
    print("ℹ️  This is a dry run. No resources were created or modified.")
    print("=" * 60)


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
    on_task_complete: Optional[Callable[[TaskResult], None]] = None,
) -> ExecutionResult:
    """
    Run DAG locally.
    
    Modes:
    - mock=True: Simulate execution with mock results
    - localstack=True: Execute against LocalStack
    
    Args:
        dag: The DAG to run
        mock: Use mock execution (default: True)
        localstack: Use LocalStack (requires LocalStack running)
        localstack_endpoint: LocalStack endpoint URL
        task_mocks: Dict of task_id -> mock result
        on_task_start: Callback when task starts
        on_task_complete: Callback when task completes
    
    Returns:
        ExecutionResult with all task results
    
    Example:
        # Simple mock run
        result = run(dag)
        print(result.summary())
        
        # With custom mocks
        result = run(dag, task_mocks={
            'extract': {'records': 100},
            'transform': {'records': 95},
        })
        
        # With LocalStack
        result = run(dag, localstack=True)
    """
    if localstack:
        return _run_localstack(dag, localstack_endpoint, on_task_start, on_task_complete)
    else:
        return _run_mock(dag, task_mocks or {}, on_task_start, on_task_complete)


def _run_mock(
    dag: DAG,
    task_mocks: Dict[str, Any],
    on_task_start: Optional[Callable],
    on_task_complete: Optional[Callable],
) -> ExecutionResult:
    """Run with mock execution."""
    
    print("=" * 60)
    print(f"🧪 MOCK EXECUTION: {dag.dag_id}")
    print("=" * 60)
    print()
    
    start_time = datetime.now(timezone.utc)
    task_results: List[Any] = []
    task_outputs = {}  # Store outputs for downstream tasks
    
    try:
        ordered_tasks = dag.topological_sort()
    except Exception as e:
        print(f"❌ Failed to sort tasks: {e}")
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
        
        print(f"▶️  Running: {task.task_id}")
        
        # Check trigger rule
        should_run, reason = _evaluate_trigger_rule(task, task_results)
        
        if not should_run:
            print(f"   ⏭️  Skipped: {reason}")
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
                # Try to call the actual function
                output = task.python_callable()
            else:
                # Default mock output
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
            print(f"   ✅ Success ({result.duration_ms}ms)")
            
        except Exception as e:
            result = TaskResult(
                task_id=task.task_id,
                status='failed',
                start_time=task_start,
                end_time=datetime.now(timezone.utc),
                error=str(e),
            )
            print(f"   ❌ Failed: {e}")
        
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
    print("=" * 60)
    print(f"📊 Result: {execution_result.summary()}")
    print(f"⏱️  Duration: {execution_result.duration_ms}ms")
    print("=" * 60)
    
    return execution_result


def _run_localstack(
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
        task_results = _parse_execution_history(history['events'])
        
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
    
    for event in events:
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
            for e in reversed(events[:events.index(event)]):
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

def run_cli(dag_file: str, mode: str = 'dry-run', **kwargs):
    """
    Run from CLI.
    
    Args:
        dag_file: Path to Python file containing DAG
        mode: One of 'dry-run', 'validate', 'mock', 'localstack'
    """
    import importlib.util
    
    # Load DAG from file
    spec = importlib.util.spec_from_file_location("dag_module", dag_file)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: spec_from_file_location returns a loaded spec for existing .py paths
        raise ImportError(f"Cannot load module from {dag_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    # Find DAG in module
    dag = None
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, DAG):
            dag = obj
            break
    
    if not dag:
        print(f"❌ No DAG found in {dag_file}")
        return
    
    if mode == 'dry-run':
        dry_run(dag, **kwargs)
    elif mode == 'validate':
        validate(dag, **kwargs)
    elif mode == 'mock':
        run(dag, mock=True, **kwargs)
    elif mode == 'localstack':
        run(dag, localstack=True, **kwargs)
    else:
        print(f"❌ Unknown mode: {mode}")
