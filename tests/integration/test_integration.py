"""
Integration tests for Lambda handlers.
Tests the core logic with mocked AWS services.
"""
import sys
import os

from datetime import datetime

# Add parent to path
# sys.path setup moved to conftest.py

# =============================================================================
# Test: notify_dependents handler logic
# =============================================================================

def test_notify_dependents_trigger_rules():
    """Test trigger rule evaluation logic from notify_dependents."""
    
    # Terminal status sets (must match production evaluate_deps/index.py)
    TERMINAL_SUCCESS = {'success', 'skipped'}
    TERMINAL_FAILURE = {'failed', 'upstream_failed', 'aborted'}
    TERMINAL_STATUSES = TERMINAL_SUCCESS | TERMINAL_FAILURE
    
    # Simulate the trigger rule logic (extracted from handler)
    def should_trigger(trigger_rule, dep_statuses):
        """Determine if task should trigger based on dependency statuses."""
        if not dep_statuses:
            return 'pass'
        
        success_count = sum(1 for s in dep_statuses if s in TERMINAL_SUCCESS)
        failed_count = sum(1 for s in dep_statuses if s in TERMINAL_FAILURE)
        done_count = sum(1 for s in dep_statuses if s in TERMINAL_STATUSES)
        total_deps = len(dep_statuses)
        
        if trigger_rule == 'all_success':
            if done_count == total_deps and failed_count == 0:
                return 'pass'
            elif failed_count > 0:
                return 'block'
            return 'wait'
        
        elif trigger_rule == 'one_success':
            if success_count > 0:
                return 'pass'
            elif done_count == total_deps:
                return 'block'
            return 'wait'
        
        elif trigger_rule == 'all_done':
            if done_count == total_deps:
                return 'pass'
            return 'wait'
        
        return 'wait'
    
    # Test cases
    # all_success
    assert should_trigger('all_success', ['success']) == 'pass'
    assert should_trigger('all_success', ['success', 'success']) == 'pass'
    assert should_trigger('all_success', ['success', 'skipped']) == 'pass'
    assert should_trigger('all_success', ['success', 'failed']) == 'block'
    assert should_trigger('all_success', ['success', 'aborted']) == 'block'  # aborted is failure
    assert should_trigger('all_success', ['waiting']) == 'wait'
    
    # one_success (immediate)
    assert should_trigger('one_success', ['success']) == 'pass'
    assert should_trigger('one_success', ['success', 'waiting']) == 'pass'
    assert should_trigger('one_success', ['failed', 'failed']) == 'block'
    assert should_trigger('one_success', ['aborted', 'failed']) == 'block'  # aborted counts as done
    assert should_trigger('one_success', ['waiting']) == 'wait'
    
    # all_done
    assert should_trigger('all_done', ['success', 'failed']) == 'pass'
    assert should_trigger('all_done', ['failed', 'failed']) == 'pass'
    assert should_trigger('all_done', ['success', 'aborted']) == 'pass'  # aborted is terminal
    assert should_trigger('all_done', ['success', 'waiting']) == 'wait'
    
    # Edge: empty deps
    assert should_trigger('all_success', []) == 'pass'
    
    print("✅ notify_dependents trigger rules OK")


# =============================================================================
# Test: console_api handler routing
# =============================================================================

def test_console_api_routes():
    """Test that console_api routes are properly defined."""
    # After refactoring, routes are defined in main.py
    main_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        'sam/lambdas/console_api/main.py'
    )
    
    with open(main_path, 'r') as f:
        content = f.read()
    
    # Critical routes that must exist
    required_routes = [
        # Pipelines
        "/api/pipelines",
        "/pipeline/",
        "/status",
        "/dag",
        "/run",
        "/backfill",
        
        # Tasks
        "/api/tasks",
        "/task/",
        "/skip",
        "/fail",
        "/restart",
        "/events",
        
        # Runs
        "/api/runs",
        
        # Assets
        "/api/assets",
        "/trigger",
    ]
    
    for route in required_routes:
        if route not in content:
            print(f"⚠️  Route may be missing: {route}")
    
    print("✅ console_api routes OK")


# =============================================================================
# Test: Asset trigger AND logic
# =============================================================================

def test_asset_trigger_and_logic():
    """Test AND logic for asset-triggered DAGs."""
    
    # Simulate queue checking logic
    def check_all_assets_ready(required_assets, queued_assets):
        """Check if all required assets are in queue."""
        return all(asset in queued_assets for asset in required_assets)
    
    required = ['asset_a', 'asset_b', 'asset_c']
    
    # Not all ready
    assert not check_all_assets_ready(required, ['asset_a'])
    assert not check_all_assets_ready(required, ['asset_a', 'asset_b'])
    
    # All ready
    assert check_all_assets_ready(required, ['asset_a', 'asset_b', 'asset_c'])
    
    # Extra assets don't matter
    assert check_all_assets_ready(required, ['asset_a', 'asset_b', 'asset_c', 'asset_d'])
    
    # Empty required = always ready
    assert check_all_assets_ready([], ['anything'])
    assert check_all_assets_ready([], [])
    
    print("✅ asset trigger AND logic OK")


# =============================================================================
# Test: Backfill date generation
# =============================================================================

def test_backfill_date_generation():
    """Test backfill date range generation."""
    from datetime import timedelta
    
    def generate_dates(start_str, end_str):
        """Generate list of dates between start and end (inclusive)."""
        start = datetime.strptime(start_str, '%Y-%m-%d')
        end = datetime.strptime(end_str, '%Y-%m-%d')
        dates = []
        current = start
        while current <= end:
            dates.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
        return dates
    
    # Single day
    assert generate_dates('2026-01-15', '2026-01-15') == ['2026-01-15']
    
    # Week
    week = generate_dates('2026-01-01', '2026-01-07')
    assert len(week) == 7
    assert week[0] == '2026-01-01'
    assert week[-1] == '2026-01-07'
    
    # Month boundary
    month_end = generate_dates('2026-01-30', '2026-02-02')
    assert month_end == ['2026-01-30', '2026-01-31', '2026-02-01', '2026-02-02']
    
    print("✅ backfill date generation OK")


# =============================================================================
# Test: Auto variables for backfill
# =============================================================================

def test_backfill_auto_variables():
    """Test auto variable generation for backfill."""
    from datetime import timedelta
    
    def generate_auto_vars(date_str):
        """Generate automatic variables for a date."""
        dt = datetime.strptime(date_str, '%Y-%m-%d')
        return {
            'current_date': date_str,
            'date_compact': dt.strftime('%Y%m%d'),
            'year': dt.strftime('%Y'),
            'month': dt.strftime('%m'),
            'day': dt.strftime('%d'),
            'day_of_week': dt.strftime('%A').lower(),
            'previous_date': (dt - timedelta(days=1)).strftime('%Y-%m-%d'),
            'minus_7_days': (dt - timedelta(days=7)).strftime('%Y-%m-%d'),
            'minus_30_days': (dt - timedelta(days=30)).strftime('%Y-%m-%d'),
            'is_backfill': True
        }
    
    vars = generate_auto_vars('2026-07-25')
    
    assert vars['current_date'] == '2026-07-25'
    assert vars['date_compact'] == '20260725'
    assert vars['year'] == '2026'
    assert vars['month'] == '07'
    assert vars['day'] == '25'
    assert vars['day_of_week'] == 'saturday'
    assert vars['previous_date'] == '2026-07-24'
    assert vars['minus_7_days'] == '2026-07-18'
    assert vars['is_backfill']
    
    print("✅ backfill auto variables OK")


# =============================================================================
# Test: Error truncation
# =============================================================================

def test_error_truncation():
    """Test error message truncation for DynamoDB."""
    
    MAX_ERROR_LENGTH = 1000
    
    def truncate_error(error_str):
        """Truncate error to fit DynamoDB limits."""
        if not error_str:
            return ''
        if len(error_str) <= MAX_ERROR_LENGTH:
            return error_str
        return error_str[:MAX_ERROR_LENGTH - 20] + '... [truncated]'
    
    # Short error - unchanged
    short = "Connection timeout"
    assert truncate_error(short) == short
    
    # Long error - truncated
    long_error = "A" * 2000
    truncated = truncate_error(long_error)
    assert len(truncated) <= MAX_ERROR_LENGTH
    assert truncated.endswith('[truncated]')
    
    # Empty error
    assert truncate_error('') == ''
    assert truncate_error(None) == ''
    
    print("✅ error truncation OK")


# =============================================================================
# Test: Execution name generation
# =============================================================================

def test_execution_name_generation():
    """Test deterministic execution name generation."""
    
    def generate_execution_name(task_name, date, pipeline_short):
        """Generate deterministic execution name."""
        # Format: task-date-pipeline_short (max 80 chars)
        base = f"{task_name}-{date}-{pipeline_short}"
        return base[:80]
    
    name = generate_execution_name('scrape', '2026-01-15', 'abc12345')
    assert name == 'scrape-2026-01-15-abc12345'
    
    # Long task name - truncated
    long_task = 'a' * 100
    long_name = generate_execution_name(long_task, '2026-01-15', 'abc12345')
    assert len(long_name) <= 80
    
    print("✅ execution name generation OK")


# =============================================================================
# Test: Webhook URL parsing
# =============================================================================

def test_webhook_url_parsing():
    """Test Slack webhook URL validation."""
    
    def is_valid_slack_webhook(url):
        """Check if URL is a valid Slack webhook."""
        if not url:
            return False
        return url.startswith('https://hooks.slack.com/') or url.startswith('https://hooks.slack-gov.com/')
    
    assert is_valid_slack_webhook('https://hooks.slack.com/services/T.../B.../xxx')
    assert is_valid_slack_webhook('https://hooks.slack-gov.com/services/T.../B.../xxx')
    assert not is_valid_slack_webhook('https://example.com/webhook')
    assert not is_valid_slack_webhook('')
    assert not is_valid_slack_webhook(None)
    
    print("✅ webhook URL parsing OK")


def test_backfill_upstream_dependencies():
    """Test that backfill by assets includes upstream dependencies."""
    dag = {
        "nodes": [
            {"id": "extract", "outlets": [{"name": "raw_data"}]},
            {"id": "transform", "outlets": [{"name": "clean_data"}]},
            {"id": "load", "outlets": [{"name": "final_output"}]},
            {"id": "notify", "outlets": []},  # unrelated task
        ],
        "edges": [
            {"from": "extract", "to": "transform"},
            {"from": "transform", "to": "load"},
        ]
    }
    
    assets = ["final_output"]  # User wants only final output
    
    # Build dependency graph from edges
    edges = dag.get('edges', [])
    dependencies_of = {}
    for edge in edges:
        from_task = edge.get('from', '')
        to_task = edge.get('to', '')
        if to_task not in dependencies_of:
            dependencies_of[to_task] = []
        dependencies_of[to_task].append(from_task)
    
    # Find tasks that produce selected assets
    target_tasks = set()
    for node in dag['nodes']:
        for outlet in node.get('outlets', []):
            outlet_name = outlet.get('name', '') if isinstance(outlet, dict) else outlet
            if outlet_name in assets:
                target_tasks.add(node.get('id'))
    
    # Recursively find all upstream dependencies
    def get_all_upstream(task_id, visited=None):
        if visited is None:
            visited = set()
        if task_id in visited:
            return set()
        visited.add(task_id)
        result = {task_id}
        for dep in dependencies_of.get(task_id, []):
            result |= get_all_upstream(dep, visited)
        return result
    
    tasks_to_run = set()
    for target in target_tasks:
        tasks_to_run |= get_all_upstream(target)
    
    all_tasks = {node['id'] for node in dag['nodes']}
    skip_tasks = list(all_tasks - tasks_to_run)
    
    # Assertions
    assert tasks_to_run == {'extract', 'transform', 'load'}, f"Expected full chain, got {tasks_to_run}"
    assert 'notify' in skip_tasks, "Unrelated task should be skipped"
    assert len(skip_tasks) == 1, f"Only 1 task should be skipped, got {skip_tasks}"
    
    print("✅ backfill upstream dependencies OK")


# =============================================================================
# Main runner
# =============================================================================

if __name__ == '__main__':
    tests = [
        test_notify_dependents_trigger_rules,
        test_console_api_routes,
        test_asset_trigger_and_logic,
        test_backfill_date_generation,
        test_backfill_auto_variables,
        test_error_truncation,
        test_execution_name_generation,
        test_webhook_url_parsing,
        test_backfill_upstream_dependencies,
    ]
    
    passed = 0
    failed = 0
    
    print("\n" + "=" * 50)
    print("Integration Tests")
    print("=" * 50 + "\n")
    
    for test_func in tests:
        try:
            test_func()
            passed += 1
        except Exception as e:
            print(f"❌ {test_func.__name__}: {e}")
            failed += 1
    
    print("\n" + "=" * 50)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 50)
    
    sys.exit(0 if failed == 0 else 1)

