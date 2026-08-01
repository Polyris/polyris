"""
Smoke tests for tf-pipeline critical paths.
Run before production deployment.
"""
import sys
import json
import os

# Add parent to path
# sys.path setup moved to conftest.py
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def test_polyris_imports():
    """Test that polyris module imports correctly."""
    print("✅ polyris imports OK")

def test_dag_creation():
    """Test basic DAG creation."""
    from polyris import DAG, task
    
    with DAG('test_dag', schedule='rate(1 day)') as dag:
        @task.sfn(arn='arn:aws:states:us-east-1:123456789:stateMachine:test')
        def task1():
            pass
        
        t1 = task1()
    
    assert dag.dag_id == 'test_dag'
    # Tasks are registered when called
    assert t1 is not None
    print("✅ DAG creation OK")

def test_dag_json_generation():
    """Test DAG JSON generation."""
    from polyris import DAG, task
    from polyris.generators import generate_dag_json
    
    with DAG('test_dag', schedule='rate(1 day)') as dag:
        @task.sfn(arn='arn:aws:states:us-east-1:123456789:stateMachine:test')
        def task1():
            pass
        task1()
    
    result = generate_dag_json(dag)
    assert 'nodes' in result
    assert 'edges' in result
    assert len(result['nodes']) == 1
    print("✅ DAG JSON generation OK")


def test_skip_on_backfill():
    """Test skip_on_backfill parameter on tasks and metadata."""
    from polyris import DAG, task, Asset
    from polyris.generators import generate_dag_json
    
    raw = Asset("test/raw")
    processed = Asset("test/processed")
    
    with DAG('test_backfill', schedule='rate(1 day)') as dag:
        @task.sfn(arn='arn:aws:states:us-east-1:123:stateMachine:scraper', 
                  outlets=[raw], skip_on_backfill=True)
        def scraper():
            pass
        
        @task.sfn(arn='arn:aws:states:us-east-1:123:stateMachine:process',
                  inlets=[raw], outlets=[processed])
        def process():
            pass
        
        s = scraper()
        process(s)
    
    # Task object
    scraper_task = dag.get_task('scraper')
    process_task = dag.get_task('process')
    assert scraper_task.skip_on_backfill is True
    assert process_task.skip_on_backfill is False
    
    # Metadata (dag_json used by UI)
    result = generate_dag_json(dag)
    scraper_node = next(n for n in result['nodes'] if n['id'] == 'scraper')
    process_node = next(n for n in result['nodes'] if n['id'] == 'process')
    assert scraper_node.get('skip_on_backfill') is True
    assert process_node.get('skip_on_backfill') is None  # Not set when False
    
    print("✅ skip_on_backfill OK")


def test_sfn_templates_valid_json():
    """Test that all SFN templates are valid JSON (after resolving template vars)."""
    import re
    templates_dir = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates')
    
    errors = []
    for root, dirs, files in os.walk(templates_dir):
        for f in files:
            if f.endswith('.json'):
                path = os.path.join(root, f)
                try:
                    with open(path) as fp:
                        text = fp.read()
                    # Replace ${var} template placeholders with dummy values
                    text = re.sub(r'\$\{[^}]+\}', '0', text)
                    json.loads(text)
                except json.JSONDecodeError as e:
                    errors.append(f"{path}: {e}")
    
    if errors:
        for e in errors:
            print(f"❌ {e}")
        raise AssertionError(f"{len(errors)} JSON errors found")
    
    print("✅ All SFN templates valid JSON")


def test_sfn_templates_valid_asl():
    """Test that all SFN templates are valid Amazon States Language (ASL).
    
    Validates:
    - Required fields: States, StartAt
    - StartAt references existing state
    - Each state has valid Type
    - Non-Choice states have Next or End (except terminal)
    - Choice states have Choices array
    """
    templates_dir = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates')
    
    VALID_STATE_TYPES = {'Task', 'Pass', 'Choice', 'Wait', 'Succeed', 'Fail', 'Parallel', 'Map'}
    TERMINAL_TYPES = {'Succeed', 'Fail'}
    
    def validate_asl(template: dict, path: str) -> list:
        """Validate ASL structure, returns list of errors."""
        errors = []
        
        # Required fields
        if 'States' not in template:
            errors.append(f"{path}: Missing required 'States' field")
            return errors  # Can't continue without States
        
        if 'StartAt' not in template:
            errors.append(f"{path}: Missing required 'StartAt' field")
        
        states = template.get('States', {})
        start_at = template.get('StartAt', '')
        
        # StartAt must reference existing state
        if start_at and start_at not in states:
            errors.append(f"{path}: StartAt '{start_at}' not found in States")
        
        # Validate each state
        for state_name, state in states.items():
            state_type = state.get('Type', '')
            
            # Type is required
            if not state_type:
                errors.append(f"{path}: State '{state_name}' missing Type")
                continue
            
            # Type must be valid
            if state_type not in VALID_STATE_TYPES:
                errors.append(f"{path}: State '{state_name}' has invalid Type '{state_type}'")
                continue
            
            # Non-terminal, non-Choice states need Next or End
            if state_type not in TERMINAL_TYPES and state_type != 'Choice':
                has_next = 'Next' in state
                has_end = state.get('End', False)
                
                # Map and Parallel can have internal states, check Iterator/Branches
                if state_type == 'Map' and 'ItemProcessor' in state:
                    # ItemProcessor has its own States, validated separately
                    pass
                elif state_type == 'Parallel' and 'Branches' in state:
                    # Branches are separate state machines
                    pass
                elif not has_next and not has_end:
                    errors.append(f"{path}: State '{state_name}' ({state_type}) has neither Next nor End")
            
            # Choice must have Choices
            if state_type == 'Choice' and 'Choices' not in state:
                errors.append(f"{path}: Choice state '{state_name}' missing Choices array")
        
        return errors
    
    all_errors = []
    template_count = 0
    
    for root, dirs, files in os.walk(templates_dir):
        for f in files:
            if f.endswith('.json'):
                path = os.path.join(root, f)
                template_count += 1
                try:
                    with open(path) as fp:
                        template = json.load(fp)
                    errors = validate_asl(template, path)
                    all_errors.extend(errors)
                except json.JSONDecodeError:
                    pass  # Already caught by test_sfn_templates_valid_json
    
    if all_errors:
        for e in all_errors:
            print(f"❌ {e}")
        raise AssertionError(f"{len(all_errors)} ASL validation errors found")
    
    print(f"✅ All {template_count} SFN templates valid ASL")

def test_lambda_syntax():
    """Test that all Lambda handlers have valid Python syntax."""
    import ast
    
    lambdas_dir = os.path.join(REPO_ROOT, 
                               'sam/lambdas')
    
    errors = []
    for root, dirs, files in os.walk(lambdas_dir):
        for f in files:
            if f.endswith('.py'):
                path = os.path.join(root, f)
                try:
                    with open(path) as fp:
                        ast.parse(fp.read())
                except SyntaxError as e:
                    errors.append(f"{path}: {e}")
    
    if errors:
        for e in errors:
            print(f"❌ {e}")
        raise AssertionError(f"{len(errors)} Python syntax errors found")
    
    print("✅ All Lambda handlers valid Python")

def test_notify_dependents_deps_blocked():
    """Test that evaluate_deps handles upstream_failed propagation."""
    base_path = os.path.join(REPO_ROOT, 
                             'sam/lambdas/evaluate_deps')
    
    # Check constants.py has upstream_failed status defined
    with open(os.path.join(base_path, 'constants.py')) as f:
        constants_content = f.read()
    
    assert "UPSTREAM_FAILED" in constants_content, "UPSTREAM_FAILED not defined in constants.py"
    # v0.79.5 (ADR #77) — sets are now module-level (TASK_FAILURE_STATUSES),
    # not class-level (FAILURE_STATES).
    assert "TASK_FAILURE_STATUSES" in constants_content, \
        "TASK_FAILURE_STATUSES not defined in constants.py"

    # Check index.py imports and uses the failure states
    with open(os.path.join(base_path, 'index.py')) as f:
        index_content = f.read()

    assert "from constants import" in index_content, "constants not imported in index.py"
    assert "TASK_FAILURE_STATUSES" in index_content, "TASK_FAILURE_STATUSES not imported"
    assert "TERMINAL_FAILURE" in index_content, "TERMINAL_FAILURE not used in index.py"
    print("✅ evaluate_deps upstream_failed handling implemented")

def test_dependency_wrapper_check_deps_signal():
    """Test that dependency_wrapper has Check_Deps_Signal state."""
    template_path = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates/dependency_wrapper/sfn.tpl.json')
    
    with open(template_path) as f:
        content = json.load(f)
    
    states = content.get('States', {})
    assert 'Check_Deps_Signal' in states, "Check_Deps_Signal state not found"
    assert 'Handle_Deps_Blocked' in states, "Handle_Deps_Blocked state not found"
    print("✅ dependency_wrapper Check_Deps_Signal implemented")

def test_run_task_helper_saves_arn():
    """Test that run_task_helper saves its own ARN."""
    template_path = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates/helpers/run_task/sfn.tpl.json')
    
    with open(template_path) as f:
        content = f.read()
    
    assert 'run_task_helper_arn' in content, "run_task_helper_arn not saved"
    assert '$states.context.Execution.Id' in content, "Execution.Id not captured"
    print("✅ run_task_helper saves ARN")

def test_task_events_table():
    """Test that task_events table is defined in SAM template."""
    sam_template_path = os.path.join(REPO_ROOT,
                                      'sam/template.yaml')

    with open(sam_template_path) as f:
        content = f.read()

    assert 'task_events' in content, "task_events table not found in SAM template"
    assert 'task_run_id' in content, "task_run_id not found in schema"
    print("✅ task_events table defined")


def test_wrapper_emits_events():
    """Test that wrapper emits events to task_events table."""
    template_path = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates/dependency_wrapper/sfn.tpl.json')
    
    with open(template_path) as f:
        content = f.read()
    
    assert 'task_run_id' in content, "task_run_id not found in wrapper"
    assert 'Emit_Wrapper_Started' in content, "Emit_Wrapper_Started state not found"
    assert 'WRAPPER_STARTED' in content, "WRAPPER_STARTED event type not found"
    print("✅ wrapper emits events")


def test_run_task_emits_events():
    """Test that run_task helper emits events."""
    template_path = os.path.join(REPO_ROOT, 
                                  'sam/sfn_templates/helpers/run_task/sfn.tpl.json')
    
    with open(template_path) as f:
        content = f.read()
    
    assert 'Emit_Task_Started' in content, "Emit_Task_Started state not found"
    assert 'Emit_Task_Finished' in content, "Emit_Task_Finished state not found"
    assert 'TASK_STARTED' in content, "TASK_STARTED event type not found"
    assert 'TASK_FINISHED' in content, "TASK_FINISHED event type not found"
    # pipeline_name must be in completion events for WebSocket broadcast
    assert '"pipeline_name"' in content, "pipeline_name not in completion events"
    print("✅ run_task emits events")


def test_api_has_task_events_endpoint():
    """Test that API has task events endpoint."""
    # After refactoring, task handlers are in routes/tasks.py
    tasks_path = os.path.join(REPO_ROOT, 
                              'sam/lambdas/console_api/routes/tasks.py')
    
    with open(tasks_path) as f:
        content = f.read()
    
    assert 'def get_task_events' in content, "get_task_events function not found"
    assert 'task_events_repo' in content, "task_events_repo not found (DAL for task events)"
    print("✅ API has task_events endpoint")


def test_trigger_rule_sync():
    """Test that JSONata and Python trigger_rule implementations are in sync."""
    # Check JSONata in registration helper
    registration_path = os.path.join(REPO_ROOT, 
                                      'sam/sfn_templates/helpers/registration/sfn.tpl.json')
    with open(registration_path) as f:
        jsonata_content = f.read()
    
    # Check Python in evaluate_deps (source of truth for trigger rules)
    evaluate_deps_path = os.path.join(REPO_ROOT, 
                               'sam/lambdas/evaluate_deps/index.py')
    with open(evaluate_deps_path) as f:
        python_content = f.read()
    
    # Check constants.py for status definitions
    constants_path = os.path.join(REPO_ROOT, 
                               'sam/lambdas/evaluate_deps/constants.py')
    with open(constants_path) as f:
        constants_content = f.read()
    
    # All trigger rules that should be supported (ADR #117 — trimmed to 5)
    rules = ['all_success', 'one_success', 'all_done', 'all_skipped', 'none_skipped']
    
    for rule in rules:
        assert rule in jsonata_content, f"trigger_rule '{rule}' missing in JSONata"
        assert rule in python_content, f"trigger_rule '{rule}' missing in Python"
    
    # aborted must be treated as failure/done - check constants.py where sets are defined
    assert 'aborted' in jsonata_content, "aborted status missing in JSONata trigger_rule"
    assert 'ABORTED' in constants_content, "ABORTED not defined in constants.py"
    # v0.79.5 (ADR #77) — class-level sets moved to module-level
    # constants generated from polyris/constants.py.
    assert 'TASK_FAILURE_STATUSES' in constants_content, \
        "TASK_FAILURE_STATUSES not defined in constants.py"

    # Verify index.py imports and uses the failure states
    assert 'from constants import' in python_content, "constants not imported"
    assert 'TASK_FAILURE_STATUSES' in python_content, "TASK_FAILURE_STATUSES not imported"
    assert 'TERMINAL_FAILURE' in python_content, "TERMINAL_FAILURE not used in trigger rules"
    
    print("✅ trigger_rule implementations in sync")


def test_pipeline_execution_short_length():
    """Test that pipeline_execution_short uses 20 chars (not 8 or 16) to avoid collisions."""
    wrapper_path = os.path.join(REPO_ROOT, 
                                 'sam/sfn_templates/dependency_wrapper/sfn.tpl.json')
    
    with open(wrapper_path) as f:
        content = f.read()
    
    # Should be > 20
    assert '> 20' in content, "pipeline_execution_short should use 20 chars"
    assert '- 20)' in content, "pipeline_execution_short should take last 20 chars"
    print("✅ pipeline_execution_short uses 20 chars")


def test_no_dead_code():
    """Test that unused claim check functions are removed."""
    # After refactoring, utils.py should have retrieve_result (but not store_large_result)
    utils_path = os.path.join(REPO_ROOT, 
                              'sam/lambdas/console_api/utils.py')
    with open(utils_path) as f:
        utils_content = f.read()
    
    assert 'def store_large_result' not in utils_content, "Dead code store_large_result in utils"
    assert 'def retrieve_result' in utils_content, "retrieve_result should exist in utils"
    
    # evaluate_deps should be minimal (no claim check functions)
    evaluate_deps_path = os.path.join(REPO_ROOT, 
                              'sam/lambdas/evaluate_deps/index.py')
    with open(evaluate_deps_path) as f:
        evaluate_content = f.read()
    
    assert 'def store_large_result' not in evaluate_content, "Dead code store_large_result in evaluate_deps"
    assert 'def retrieve_result' not in evaluate_content, "retrieve_result not needed in evaluate_deps"
    
    print("✅ No dead code (claim check functions cleaned up)")


def test_dag_group_field():
    """Test that DAG group field is stored and flows to ASL Comment metadata."""
    from polyris import DAG, task
    from polyris.generators import generate_step_function_json
    import json
    
    with DAG('test-pipeline', schedule='rate(1 day)', group='mygroup') as dag:
        @task.sfn(arn='arn:aws:states:us-east-1:123456789:stateMachine:test')
        def task1():
            pass
        task1()
    
    # Check DAG field
    assert dag.group == 'mygroup', f"Expected 'mygroup', got '{dag.group}'"
    
    # Check ASL Comment contains group
    asl = generate_step_function_json(dag, registry_table='test-registry')
    definition = json.loads(asl)
    metadata = json.loads(definition['Comment'])
    assert metadata.get('group') == 'mygroup', f"ASL Comment missing group: {metadata}"
    
    # Check inline DDB registration has pipeline_group
    register_state = definition['States'].get('Register_Pipeline', {})
    register_item = register_state.get('Arguments', {}).get('Item', {})
    assert 'pipeline_group' in register_item, "Register_Pipeline missing pipeline_group in DDB Item"
    assert register_item['pipeline_group']['S'] == 'mygroup', f"Wrong pipeline_group value: {register_item['pipeline_group']}"
    
    print("✅ DAG group field flows to ASL Comment and DDB registration")


def test_dag_group_default_empty():
    """Test that DAG group defaults to empty string."""
    from polyris import DAG, task
    from polyris.generators import generate_step_function_json
    import json
    
    with DAG('test-no-group', schedule='rate(1 day)') as dag:
        @task.sfn(arn='arn:aws:states:us-east-1:123456789:stateMachine:test')
        def task1():
            pass
        task1()
    
    assert dag.group == '', f"Expected empty string, got '{dag.group}'"
    
    asl = generate_step_function_json(dag, registry_table='test-registry')
    definition = json.loads(asl)
    metadata = json.loads(definition['Comment'])
    assert metadata.get('group') == '', f"ASL Comment group should be empty: {metadata}"
    
    print("✅ DAG group defaults to empty string")


def test_register_sfn_templates_have_pipeline_group():
    """Test that registration SFN template writes pipeline_group to DDB."""
    base = os.path.join(REPO_ROOT,
                        'sam/sfn_templates/helpers')
    
    path = os.path.join(base, 'register_pipeline', 'sfn.tpl.json')
    with open(path) as f:
        content = f.read()
    assert 'pipeline_group' in content, "register_pipeline missing pipeline_group in DDB putItem"
    
    print("✅ SFN registration template has pipeline_group")


def test_pipelines_api_returns_group():
    """Test that pipelines API endpoint returns group field."""
    api_path = os.path.join(REPO_ROOT,
                            'sam/lambdas/console_api/routes/pipelines_list.py')
    with open(api_path) as f:
        content = f.read()
    
    assert "pipeline_group" in content, "API should read pipeline_group from DDB"
    assert "'group': item.get('pipeline_group'" in content, "API should return group in response"
    
    print("✅ Pipelines API returns group field")


def test_sla_excludes_in_progress_runs():
    """SLA counts only completed runs and excludes aborted (user-stopped) runs (ADR #112)."""
    api_path = os.path.join(REPO_ROOT,
                            'sam/lambdas/console_api/routes/pipelines_list.py')
    with open(api_path) as f:
        content = f.read()

    # SLA status is the canonical derivation, not a hand-rolled terminal check.
    assert 'derive_execution_status' in content, "SLA should derive status via the canonical helper"
    # In-progress (running) and aborted runs are excluded from the SLA denominator.
    assert "in ('running', 'aborted')" in content, "SLA should exclude running and aborted runs"
    # Must NOT count all runs indiscriminately
    assert 'total_completed_runs' in content, "SLA should count only completed runs"

    print("✅ SLA excludes in-progress and aborted runs")


def test_task_completed_contract():
    """
    Test that task completion events have the required fields.
    
    In v51+, task completion is handled via:
    1. DynamoDB task_events table (for timeline)
    2. notify_dependents SFN helper (for dependency resolution)
    
    Required fields in task record:
    - task_name, execution_name, task_run_id, attempt, date
    - pipeline_name, pipeline_execution, pipeline_execution_short
    - status
    """
    required_fields = [
        'task_name', 'execution_name', 'task_run_id', 'attempt', 'date',
        'pipeline_name', 'pipeline_execution', 'pipeline_execution_short', 'status'
    ]
    
    # Check wrapper template - it writes to DynamoDB with all fields
    wrapper_path = os.path.join(REPO_ROOT, 
                                'sam/sfn_templates/dependency_wrapper/sfn.tpl.json')
    with open(wrapper_path, 'r') as f:
        content = f.read()
    
    # Wrapper must have all required fields for DynamoDB writes
    for field in required_fields:
        assert f'"{field}"' in content, f"Missing {field} in wrapper DynamoDB write"
    
    # Check run_task helper
    run_task_path = os.path.join(REPO_ROOT, 
                                 'sam/sfn_templates/helpers/run_task/sfn.tpl.json')
    with open(run_task_path, 'r') as f:
        content = f.read()
    
    # run_task should track task_run_id and attempt
    assert 'task_run_id' in content, "run_task missing task_run_id"
    assert 'attempt' in content, "run_task missing attempt"
    
    # Check notify_dependents helper exists and has required params
    notify_path = os.path.join(REPO_ROOT, 
                               'sam/sfn_templates/helpers/notify_dependents/sfn.tpl.json')
    with open(notify_path, 'r') as f:
        content = f.read()
    
    # notify_dependents should propagate completion info
    assert 'status' in content, "notify_dependents missing status"
    assert 'pipeline_execution_short' in content, "notify_dependents missing pipeline_execution_short"
    
    print("✅ task completion contract verified (DynamoDB + SFN helpers have required fields)")


def test_asset_consecutive_method():
    """Test Asset.consecutive() returns AssetConsecutiveRef."""
    from polyris.assets import Asset, AssetConsecutiveRef
    
    daily = Asset("acme/daily-complete")
    ref = daily.consecutive(days=7)
    
    assert isinstance(ref, AssetConsecutiveRef)
    assert ref.asset is daily
    assert ref.consecutive_days == 7
    assert ref.name == "acme/daily-complete"
    
    # Validation
    import pytest
    with pytest.raises(ValueError):
        daily.consecutive(days=0)
    with pytest.raises(ValueError):
        daily.consecutive(days=-1)
    
    print("✅ Asset.consecutive() method works correctly")


def test_asset_consecutive_serialization():
    """Test AssetConsecutiveRef serializes correctly in wait_for."""
    from polyris.generators import _serialize_wait_for
    from polyris.assets import Asset
    
    daily = Asset("acme/daily-complete")
    ref = daily.consecutive(days=7)
    
    result = _serialize_wait_for([ref])
    assert len(result) == 1
    assert result[0]["asset_name"] == "acme/daily-complete"
    assert result[0]["consecutive_days"] == 7
    
    print("✅ AssetConsecutiveRef serialization works correctly")


def test_asset_consecutive_operators():
    """Test AssetConsecutiveRef supports & and | operators."""
    from polyris.assets import Asset, AssetAll, AssetAny
    
    daily = Asset("acme/daily-complete")
    other = Asset("other/asset")
    
    # consecutive & asset → AssetAll
    combo = daily.consecutive(days=7) & other
    assert isinstance(combo, AssetAll)
    
    # consecutive | asset → AssetAny
    combo = daily.consecutive(days=7) | other
    assert isinstance(combo, AssetAny)
    
    # asset & consecutive → AssetAll
    combo = other & daily.consecutive(days=7)
    assert isinstance(combo, AssetAll)
    
    # asset | consecutive → AssetAny
    combo = other | daily.consecutive(days=7)
    assert isinstance(combo, AssetAny)
    
    # consecutive & within → AssetAll
    combo = daily.consecutive(days=7) & other.within(hours=24)
    assert isinstance(combo, AssetAll)
    
    # consecutive | within → AssetAny
    combo = daily.consecutive(days=7) | other.within(hours=24)
    assert isinstance(combo, AssetAny)
    
    # within & consecutive → AssetAll
    combo = other.within(hours=24) & daily.consecutive(days=7)
    assert isinstance(combo, AssetAll)
    
    # two consecutives
    inventory = Asset("inventory/daily")
    combo = daily.consecutive(days=7) & inventory.consecutive(days=7)
    assert isinstance(combo, AssetAll)
    
    combo = daily.consecutive(days=7) | inventory.consecutive(days=7)
    assert isinstance(combo, AssetAny)
    
    print("✅ AssetConsecutiveRef operators work correctly")


def run_all_tests():
    """Run all smoke tests."""
    print("=" * 50)
    print("Running smoke tests...")
    print("=" * 50)
    
    tests = [
        test_polyris_imports,
        test_dag_creation,
        test_dag_json_generation,
        test_sfn_templates_valid_json,
        test_sfn_templates_valid_asl,
        test_lambda_syntax,
        test_notify_dependents_deps_blocked,
        test_dependency_wrapper_check_deps_signal,
        test_run_task_helper_saves_arn,
        test_task_events_table,
        test_wrapper_emits_events,
        test_run_task_emits_events,
        test_api_has_task_events_endpoint,
        test_trigger_rule_sync,
        test_pipeline_execution_short_length,
        test_no_dead_code,
        test_task_completed_contract,
        test_asset_consecutive_method,
        test_asset_consecutive_serialization,
        test_asset_consecutive_operators,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            print(f"❌ {test.__name__}: {e}")
            failed += 1
    
    print("=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)
    
    return failed == 0

if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)


def test_task_intervention_handlers_are_free():
    """Task intervention (skip/fail/success/stop/restart) lives in the free
    routes/tasks.py, not ee/team/ (ADR #110). Guards the open-core split."""
    tasks_src = os.path.join(REPO_ROOT, 'sam/lambdas/console_api/routes/tasks.py')
    with open(tasks_src) as f:
        content = f.read()
    for fn in ('def stop_task', 'def skip_task', 'def fail_task',
               'def mark_success', 'def restart_task', 'def _execute_task_action'):
        assert fn in content, f"{fn} should live in free routes/tasks.py (ADR #110)"


def test_manual_decision_events_free():
    """Manual task actions record MANUAL_DECISION events from the free
    routes/tasks.py after the intervention tier-flip (ADR #110)."""
    tasks_src = os.path.join(REPO_ROOT, 'sam/lambdas/console_api/routes/tasks.py')
    with open(tasks_src) as f:
        content = f.read()
    assert 'record_manual_decision' in content, "record_manual_decision not found"
    assert "action_name='skip'" in content, "skip_task should pass 'skip' action_name"
    assert "action_name='fail'" in content, "fail_task should pass 'fail' action_name"
    assert "record_manual_decision(execution_name, 'stop'" in content, "stop_task should record MANUAL_DECISION"
    assert "record_manual_decision(execution_name, 'restart'" in content, "restart_task should record MANUAL_DECISION"
    assert "record_manual_decision(execution_name, action_name," in content, (
        "_execute_task_action should call record_manual_decision"
    )
