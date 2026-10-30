"""
Tests for polyris/local.py — validate, dry_run, run, CLI dispatch.

Coverage targets (after removing local.py from the omit list):
- TaskResult / ExecutionResult data classes
- validate() delegation to validate_asl_from_dag
- dry_run() all branches (show_asl, show_mermaid, topo-sort error)
- run() mock path
- _run_mock() success / failed / skipped / callbacks / python_callable / statuses
- _evaluate_trigger_rule() all_done "not all done" branch (everything else is
  covered by the pre-existing test_local_trigger_rules.py)
- _parse_execution_history() all event types
- _load_dag_from_file() success / no-dag / load-error / exit-0 / exit-1
- run_cli() all modes
- main() all non-localstack modes

_run_localstack() body and the localstack dispatch lines carry
# pragma: no cover — they require a live LocalStack instance and are
exercised by e2e/smoke tests instead.
"""
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from polyris import DAG, task
from polyris.local import (
    TaskResult,
    ExecutionResult,
    validate,
    dry_run,
    run,
    _run_mock,
    _evaluate_trigger_rule,
    _parse_execution_history,
    _load_dag_from_file,
    run_cli,
    main,
)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def _result(task_id, status):
    now = _now()
    return TaskResult(task_id=task_id, status=status, start_time=now, end_time=now)


def _dep(node_id):
    return SimpleNamespace(node_id=node_id)


def _fake_task(trigger_rule, dep_ids):
    return SimpleNamespace(trigger_rule=trigger_rule, dependencies=[_dep(d) for d in dep_ids])


def _make_dag_file(tmp_path, name="test-pipeline", arns_real=True):
    """Write a simple 2-task pipeline file. arns_real controls whether ARNs are
    real (valid) or still contain REGION/ACCOUNT_ID placeholders."""
    if arns_real:
        arn_a = "arn:aws:states:us-east-1:123456789012:stateMachine:extract"
        arn_b = "arn:aws:states:us-east-1:123456789012:stateMachine:load"
    else:
        arn_a = "arn:aws:states:REGION:ACCOUNT_ID:stateMachine:extract"
        arn_b = "arn:aws:states:REGION:ACCOUNT_ID:stateMachine:load"
    code = f'''
from polyris import DAG, task

with DAG("{name}", schedule="@daily") as dag:
    @task.sfn(arn="{arn_a}")
    def extract(): pass

    @task.sfn(arn="{arn_b}")
    def load(): pass

    extract() >> load()
'''
    f = tmp_path / "dag.py"
    f.write_text(code)
    return str(f)


def _make_python_dag_file(tmp_path):
    """Pipeline whose python_callable returns a value (sfn tasks wrap the function)."""
    code = '''
from polyris import DAG, task

with DAG("py-pipeline", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:compute")
    def compute():
        return {"result": 42}

    compute()
'''
    f = tmp_path / "dag.py"
    f.write_text(code)
    return str(f)


def _make_failing_dag_file(tmp_path):
    """Pipeline whose python_callable raises, so _run_mock produces a failed task."""
    code = '''
from polyris import DAG, task

with DAG("fail-pipeline", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:bad")
    def bad():
        raise ValueError("intentional failure")

    bad()
'''
    f = tmp_path / "dag.py"
    f.write_text(code)
    return str(f)


# ─── TaskResult ───────────────────────────────────────────────────────────────

class TestTaskResult:
    def test_duration_ms(self):
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
        r = TaskResult("t", "success", start, end)
        assert r.duration_ms == 1000

    def test_duration_ms_sub_second(self):
        start = datetime(2024, 1, 1, 0, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 0, 0, 500000, tzinfo=timezone.utc)
        r = TaskResult("t", "success", start, end)
        assert r.duration_ms == 500


# ─── ExecutionResult ──────────────────────────────────────────────────────────

class TestExecutionResult:
    def test_duration_ms(self):
        start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        end = datetime(2024, 1, 1, 0, 0, 2, tzinfo=timezone.utc)
        r = ExecutionResult("dag", "success", start, end)
        assert r.duration_ms == 2000

    def test_summary_mixed(self):
        now = _now()
        tasks = [
            TaskResult("a", "success", now, now),
            TaskResult("b", "failed", now, now),
            TaskResult("c", "skipped", now, now),
        ]
        r = ExecutionResult("dag", "partial", now, now, task_results=tasks)
        s = r.summary()
        assert "1" in s  # 1 succeeded, 1 failed, 1 skipped

    def test_summary_all_success(self):
        now = _now()
        tasks = [TaskResult("a", "success", now, now), TaskResult("b", "success", now, now)]
        r = ExecutionResult("dag", "success", now, now, task_results=tasks)
        assert "2" in r.summary()

    def test_summary_empty(self):
        now = _now()
        r = ExecutionResult("dag", "success", now, now)
        assert "0" in r.summary()


# ─── validate() ───────────────────────────────────────────────────────────────

class TestValidate:
    def test_valid_dag_returns_valid_true(self, tmp_path):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        result = validate(dag, verbose=False)
        assert result['valid'] is True
        assert result['errors'] == []
        assert 'dag_id' in result['info']
        assert result['info']['task_count'] == 2

    def test_placeholder_dag_returns_valid_false(self, tmp_path):
        dag_file = _make_dag_file(tmp_path, arns_real=False)
        dag = _load_dag_from_file(dag_file)
        result = validate(dag, verbose=False)
        assert result['valid'] is False
        assert any("placeholder" in e.lower() for e in result['errors'])

    def test_verbose_prints_header(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        validate(dag, verbose=True)
        out = capsys.readouterr().out
        assert "🔍 Validating:" in out

    def test_info_contains_schedule(self, tmp_path):
        dag_file = _make_dag_file(tmp_path)
        dag = _load_dag_from_file(dag_file)
        result = validate(dag, verbose=False)
        assert result['info']['schedule'] == "@daily"

    def test_blank_arn_sfn_task_fails_at_load_time(self, tmp_path):
        """@task.sfn(arn='') raises ValueError at DAG-definition time — the guard
        fires before the DAG is even registered, so the file fails to load."""
        f = tmp_path / "dag.py"
        f.write_text('''\
from polyris import DAG, task
with DAG("blank-arn", schedule=None) as dag:
    @task.sfn(arn="")
    def t(): pass
    t()
''')
        with pytest.raises(SystemExit) as exc:
            _load_dag_from_file(str(f))
        assert exc.value.code == 1

    def test_cyclic_dag_returns_valid_false(self, mocker):
        """validate() must detect cycles — generate_step_function_json never
        calls topological_sort(), so the cycle check must be explicit."""
        with DAG("cycle-check", schedule=None) as dag:
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def t(): pass
            t()

        mocker.patch.object(dag, 'topological_sort', side_effect=ValueError("Cycle detected: a → b → a"))
        result = validate(dag, verbose=False)
        assert result['valid'] is False
        assert any("Cycle" in e for e in result['errors'])

    def test_step_dep_does_not_trigger_unregistered_check(self):
        """A Step in task.dependencies must NOT produce a 'not registered in this DAG'
        error — Steps live in dag.steps, not dag.tasks, and the dep-exists check must
        include dag.steps in its registered set.  The pipeline itself is invalid (direct
        step deps aren't supported), but the error must come from validate_asl_from_dag,
        not from the dep-exists guard."""
        from polyris.steps import Pass
        with DAG("step-dep", schedule=None) as dag:
            wait = Pass(step_id="wait")

            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def load(): pass

            wait >> load()

        result = validate(dag, verbose=False)
        assert result['valid'] is False
        assert not any("not registered" in e for e in result['errors'])

    def test_unregistered_dep_returns_valid_false(self):
        """A task wired via >> to a dep created outside `with DAG():` must fail
        validation — it would appear in task.dependencies but not in dag.tasks,
        producing a broken SFN template if allowed through."""
        from types import SimpleNamespace
        with DAG("unreg-dep", schedule=None) as dag:
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def t(): pass
            t()

        # Inject a foreign dep that is not in dag.tasks
        foreign = SimpleNamespace(task_id="ghost", node_id="ghost")
        dag.tasks[0].dependencies.append(foreign)  # type: ignore[attr-defined]
        result = validate(dag, verbose=False)
        assert result['valid'] is False
        assert any("not registered" in e for e in result['errors'])

    def test_unregistered_dep_via_step_returns_valid_false(self):
        """A foreign TaskInstance wired via `foreign_instance >> step` must also
        fail validation — the dep lands in step.dependencies, not task.dependencies,
        so the check must cover dag.steps' deps too."""
        from types import SimpleNamespace
        from polyris.steps import Pass
        with DAG("unreg-step-dep", schedule=None) as dag:
            Pass(step_id="wait")

            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def load(): pass
            load()

        # Inject a foreign dep into the step's dependency list
        foreign = SimpleNamespace(task_id="ghost", node_id="ghost")
        dag.steps[0].dependencies.append(foreign)  # type: ignore[attr-defined]
        result = validate(dag, verbose=False)
        assert result['valid'] is False
        assert any("not registered" in e for e in result['errors'])


# ─── dry_run() ────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_basic_output(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "extract" in out
        assert "load" in out

    def test_show_asl_includes_json(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        dry_run(dag, show_asl=True, show_mermaid=False)
        out = capsys.readouterr().out
        assert "Generated ASL" in out
        assert '"StartAt"' in out

    def test_show_mermaid_includes_diagram(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        dry_run(dag, show_asl=False, show_mermaid=True)
        out = capsys.readouterr().out
        assert "mermaid" in out

    def test_invalid_dag_shows_errors(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=False)
        dag = _load_dag_from_file(dag_file)
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert "❌" in out

    def test_non_default_trigger_rule_is_printed(self, tmp_path, capsys):
        """dry_run prints 'Trigger: <rule>' when trigger_rule != 'all_success'."""
        code = '''
from polyris import DAG, task

with DAG("tr-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:a")
    def a(): pass

    @task.sfn(
        arn="arn:aws:states:us-east-1:123456789012:stateMachine:b",
        trigger_rule="all_done",
    )
    def b(): pass

    a() >> b()
'''
        f = tmp_path / "dag.py"
        f.write_text(code)
        dag = _load_dag_from_file(str(f))
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert "Trigger: all_done" in out

    def test_blank_arn_dry_run_fails_at_load_time(self, tmp_path, capsys):
        """@task.sfn(arn='') raises ValueError at DAG-definition time — dry_run
        never gets a DAG to run because the file fails to load first."""
        f = tmp_path / "dag.py"
        f.write_text('''\
from polyris import DAG, task
with DAG("blank-arn-dry", schedule=None) as dag:
    @task.sfn(arn="")
    def t(): pass
    t()
''')
        with pytest.raises(SystemExit) as exc:
            _load_dag_from_file(str(f))
        assert exc.value.code == 1

    def test_warnings_are_printed(self, tmp_path, mocker, capsys):
        """dry_run must print one ⚠️ line per warning — two warnings → two lines."""
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        mocker.patch(
            'polyris.local.validate',
            return_value={'valid': True, 'errors': [], 'warnings': ['check alpha', 'check beta'],
                          'info': {'dag_id': 'x', 'task_count': 1, 'schedule': None}},
        )
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert out.count("⚠️") == 2
        assert "check alpha" in out
        assert "check beta" in out

    def test_topo_sort_error_is_printed(self, tmp_path, mocker, capsys):
        """dry_run catches topological_sort exceptions and prints the error."""
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        mocker.patch.object(dag, 'topological_sort', side_effect=ValueError("cycle"))
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert "❌ Error" in out

    def test_cycle_error_not_printed_twice(self, tmp_path, mocker, capsys):
        """dry_run must not reprint the cycle error in the Validation section —
        it already appeared in the Execution Order section."""
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        mocker.patch.object(dag, 'topological_sort', side_effect=ValueError("Cycle: a → b"))
        dry_run(dag, show_mermaid=False)
        out = capsys.readouterr().out
        assert out.count("Cycle: a → b") == 1

    def test_show_asl_skipped_on_cycle(self, tmp_path, mocker, capsys):
        """dry_run must not print ASL when topological_sort fails — the generated
        ASL would be misleading because generate_step_function_json ignores cycles."""
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        mocker.patch.object(dag, 'topological_sort', side_effect=ValueError("Cycle: a → b"))
        dry_run(dag, show_asl=True, show_mermaid=False)
        out = capsys.readouterr().out
        assert "Generated ASL" not in out


# ─── run() ────────────────────────────────────────────────────────────────────

class TestRun:
    def test_mock_mode_returns_execution_result(self, tmp_path):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        result = run(dag, mock=True)
        assert isinstance(result, ExecutionResult)
        assert result.status == 'success'
        assert len(result.task_results) == 2


# ─── _run_mock() ──────────────────────────────────────────────────────────────

class TestRunMock:
    def test_success_all_tasks(self, tmp_path):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        result = _run_mock(dag, {}, None, None)
        assert result.status == 'success'
        assert all(r.status == 'success' for r in result.task_results)

    def test_failed_task_status(self, tmp_path):
        dag_file = _make_failing_dag_file(tmp_path)
        dag = _load_dag_from_file(dag_file)
        result = _run_mock(dag, {}, None, None)
        assert result.status == 'failed'
        assert result.task_results[0].status == 'failed'
        assert result.task_results[0].error is not None

    def test_skipped_task_via_trigger_rule(self, tmp_path):
        """When the first task fails, a downstream all_success task is skipped."""
        code = '''
from polyris import DAG, task

with DAG("skip-test", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:bad")
    def bad():
        raise RuntimeError("fail")

    @task.sfn(
        arn="arn:aws:states:us-east-1:123456789012:stateMachine:x",
        trigger_rule="all_success",
    )
    def downstream(): pass

    bad() >> downstream()
'''
        f = tmp_path / "dag.py"
        f.write_text(code)
        dag = _load_dag_from_file(str(f))
        result = _run_mock(dag, {}, None, None)
        statuses = {r.task_id: r.status for r in result.task_results}
        assert statuses['bad'] == 'failed'
        assert statuses['downstream'] == 'skipped'
        assert result.status == 'partial'

    def test_partial_status_some_failed(self, tmp_path):
        """DAG with 1 failed + 1 success → partial (not all failed)."""
        code = '''
from polyris import DAG, task

with DAG("partial-test", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:good")
    def good(): return 1

    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:bad")
    def bad():
        raise RuntimeError("fail")

    # No chain — both run independently
    good()
    bad()
'''
        f = tmp_path / "dag.py"
        f.write_text(code)
        dag = _load_dag_from_file(str(f))
        result = _run_mock(dag, {}, None, None)
        assert result.status == 'partial'

    def test_on_task_start_callback(self, tmp_path, mocker):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        cb = mocker.stub(name="on_task_start")
        _run_mock(dag, {}, cb, None)
        assert cb.call_count == 2
        called_ids = {call.args[0] for call in cb.call_args_list}
        assert 'extract' in called_ids
        assert 'load' in called_ids

    def test_on_task_complete_callback(self, tmp_path, mocker):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        cb = mocker.stub(name="on_task_complete")
        _run_mock(dag, {}, None, cb)
        assert cb.call_count == 2
        result = cb.call_args_list[0].args[0]
        assert isinstance(result, TaskResult)

    def test_on_task_complete_called_for_skipped_task(self, tmp_path, mocker):
        """on_task_complete must fire even when a task is skipped (not only on
        success/failure) — tests the callback call at line 248 in _run_mock."""
        code = '''
from polyris import DAG, task

with DAG("cb-skip", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:bad")
    def bad():
        raise RuntimeError("fail")

    @task.sfn(
        arn="arn:aws:states:us-east-1:123456789012:stateMachine:ds",
        trigger_rule="all_success",
    )
    def ds(): pass

    bad() >> ds()
'''
        f = tmp_path / "dag.py"
        f.write_text(code)
        dag = _load_dag_from_file(str(f))
        cb = mocker.stub(name="on_task_complete")
        _run_mock(dag, {}, None, cb)
        assert cb.call_count == 2
        statuses = {call.args[0].task_id: call.args[0].status for call in cb.call_args_list}
        assert statuses['bad'] == 'failed'
        assert statuses['ds'] == 'skipped'

    def test_task_mock_dict_used(self, tmp_path):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        dag = _load_dag_from_file(dag_file)
        result = _run_mock(dag, {'extract': {'rows': 99}}, None, None)
        extract_result = next(r for r in result.task_results if r.task_id == 'extract')
        assert extract_result.output == {'rows': 99}

    def test_python_callable_is_invoked(self, tmp_path):
        dag_file = _make_python_dag_file(tmp_path)
        dag = _load_dag_from_file(dag_file)
        result = _run_mock(dag, {}, None, None)
        assert result.task_results[0].output == {"result": 42}

    def test_topo_sort_error_returns_failed(self, mocker):
        """If topological_sort raises (e.g. cycle), status is 'failed'."""
        with DAG("cycle-dag", schedule=None) as dag:
            @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
            def t(): pass
            t()

        mocker.patch.object(dag, 'topological_sort', side_effect=ValueError("cycle"))
        result = _run_mock(dag, {}, None, None)
        assert result.status == 'failed'
        assert result.task_results == []


# ─── _evaluate_trigger_rule() — all_done "not all done" branch ────────────────

class TestEvaluateTriggerRuleAllDoneMissing:
    """Covers the `return False, f"not all done"` branch in `all_done`, which
    the synchronous mock model never reaches (done_count always equals total
    there). Calling _evaluate_trigger_rule directly with fewer results than
    dependencies exercises the branch without the mock runner's constraints."""

    def test_all_done_not_all_done(self):
        task = _fake_task('all_done', ['a', 'b'])
        results = [_result('a', 'success')]  # b has no result yet
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert "not all done" in reason

    def test_all_skipped_not_all_done(self):
        """Same pattern: fewer results than deps forces the 'not all done' arm
        of the all_skipped ternary — covers the left side of
        `f"not all done..." if done_count < total else f"not all skipped..."`."""
        task = _fake_task('all_skipped', ['a', 'b'])
        results = [_result('a', 'skipped')]  # b missing
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert "not all done" in reason


# ─── _parse_execution_history() ───────────────────────────────────────────────

class TestParseExecutionHistory:
    def _ts(self):
        return datetime(2024, 1, 1, tzinfo=timezone.utc)

    def test_entered_and_exited(self):
        ts = self._ts()
        events = [
            {'type': 'TaskStateEntered', 'timestamp': ts,
             'stateEnteredEventDetails': {'name': 'extract'}},
            {'type': 'TaskStateExited', 'timestamp': ts,
             'stateExitedEventDetails': {'name': 'extract', 'output': '{"rows": 5}'}},
        ]
        results = _parse_execution_history(events)
        assert len(results) == 1
        assert results[0].task_id == 'extract'
        assert results[0].status == 'success'
        assert results[0].output == {"rows": 5}

    def test_task_failed_finds_prior_entered(self):
        ts = self._ts()
        events = [
            {'type': 'TaskStateEntered', 'timestamp': ts,
             'stateEnteredEventDetails': {'name': 'load'}},
            {'type': 'TaskFailed', 'timestamp': ts,
             'taskFailedEventDetails': {'error': 'States.TaskFailed', 'cause': 'oops'}},
        ]
        results = _parse_execution_history(events)
        assert len(results) == 1
        assert results[0].task_id == 'load'
        assert results[0].status == 'failed'
        assert "States.TaskFailed" in results[0].error

    def test_execution_failed_no_prior_entered(self):
        ts = self._ts()
        events = [
            {'type': 'ExecutionFailed', 'timestamp': ts,
             'executionFailedEventDetails': {'error': 'States.Runtime', 'cause': 'boom'}},
        ]
        results = _parse_execution_history(events)
        assert len(results) == 1
        assert results[0].task_id == 'unknown'
        assert results[0].status == 'failed'

    def test_unknown_event_type_ignored(self):
        ts = self._ts()
        events = [
            {'type': 'ExecutionStarted', 'timestamp': ts},
        ]
        results = _parse_execution_history(events)
        assert results == []


# ─── _load_dag_from_file() ────────────────────────────────────────────────────

class TestLoadDagFromFile:
    def test_loads_dag_successfully(self, tmp_path):
        dag_file = _make_dag_file(tmp_path)
        dag = _load_dag_from_file(dag_file)
        assert dag.dag_id == "test-pipeline"
        assert len(dag.tasks) == 2

    def test_no_dag_exits_1(self, tmp_path, capsys):
        f = tmp_path / "dag.py"
        f.write_text("x = 1  # no DAG here\n")
        with pytest.raises(SystemExit) as exc:
            _load_dag_from_file(str(f))
        assert exc.value.code == 1
        assert "No DAG found" in capsys.readouterr().out

    def test_load_error_exits_1(self, tmp_path, capsys):
        f = tmp_path / "dag.py"
        f.write_text("raise RuntimeError('bad import')\n")
        with pytest.raises(SystemExit) as exc:
            _load_dag_from_file(str(f))
        assert exc.value.code == 1
        assert "Error loading" in capsys.readouterr().out

    def test_sys_exit_0_is_tolerated(self, tmp_path):
        """exit(0) in a module means it ran to completion — should not abort."""
        f = tmp_path / "dag.py"
        f.write_text('''
from polyris import DAG, task

with DAG("exit0-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
    def t(): pass
    t()

import sys
sys.exit(0)
''')
        dag = _load_dag_from_file(str(f))
        assert dag.dag_id == "exit0-dag"

    def test_sys_exit_nonzero_exits_1(self, tmp_path, capsys):
        f = tmp_path / "dag.py"
        f.write_text("import sys; sys.exit(2)\n")
        with pytest.raises(SystemExit) as exc:
            _load_dag_from_file(str(f))
        assert exc.value.code == 1
        assert "Error loading" in capsys.readouterr().out

    def test_sys_exit_none_is_tolerated(self, tmp_path):
        """sys.exit() with no argument raises SystemExit(None) — must not abort."""
        f = tmp_path / "dag.py"
        f.write_text('''\
from polyris import DAG, task

with DAG("exitnone-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:x")
    def t(): pass
    t()

import sys
sys.exit()
''')
        dag = _load_dag_from_file(str(f))
        assert dag.dag_id == "exitnone-dag"

    def test_imported_dag_excluded_by_locality_filter(self, tmp_path):
        """A DAG from an imported module must not be returned when a local DAG
        exists — the locality filter uses sys.modules to exclude imported ones."""
        helper = tmp_path / "locality_helper.py"
        helper.write_text('''\
from polyris import DAG, task
with DAG("helper-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:h")
    def h(): pass
    h()
''')
        pipeline = tmp_path / "dag.py"
        pipeline.write_text(f'''\
import sys
sys.path.insert(0, {str(tmp_path)!r})
from polyris import DAG, task
with DAG("local-dag", schedule="@daily") as dag:
    @task.sfn(arn="arn:aws:states:us-east-1:123456789012:stateMachine:l")
    def l(): pass
    l()
import locality_helper  # imported after local DAG — triggers __exit__ for helper-dag
''')
        try:
            result = _load_dag_from_file(str(pipeline))
        finally:
            sys.modules.pop('locality_helper', None)
            try:
                sys.path.remove(str(tmp_path))
            except ValueError:
                pass
        assert result.dag_id == "local-dag"


# ─── run_cli() ────────────────────────────────────────────────────────────────

class TestRunCli:
    def test_dry_run_mode(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        run_cli(dag_file, mode='dry-run')
        out = capsys.readouterr().out
        assert "DRY RUN" in out

    def test_validate_mode(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        run_cli(dag_file, mode='validate')
        out = capsys.readouterr().out
        assert "🔍 Validating:" in out

    def test_mock_mode(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        run_cli(dag_file, mode='mock')
        out = capsys.readouterr().out
        assert "MOCK EXECUTION" in out

    def test_unknown_mode_prints_error(self, tmp_path, capsys):
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        run_cli(dag_file, mode='foobar')
        out = capsys.readouterr().out
        assert "Unknown mode" in out


# ─── main() ───────────────────────────────────────────────────────────────────

def _run_main(monkeypatch, tmp_path, *argv):
    dag_file = _make_dag_file(tmp_path, arns_real=True)
    monkeypatch.setattr(sys, "argv", ["polyris-local", "--file", dag_file, *argv])
    return dag_file


class TestMain:
    def test_dry_run_default_exits_0(self, tmp_path, monkeypatch):
        _run_main(monkeypatch, tmp_path)
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_dry_run_explicit_exits_0(self, tmp_path, monkeypatch):
        _run_main(monkeypatch, tmp_path, "--dry-run")
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_validate_valid_exits_0(self, tmp_path, monkeypatch):
        _run_main(monkeypatch, tmp_path, "--validate")
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_validate_invalid_exits_1(self, tmp_path, monkeypatch):
        dag_file = _make_dag_file(tmp_path, arns_real=False)
        monkeypatch.setattr(sys, "argv", ["polyris-local", "--file", dag_file, "--validate"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_mock_success_exits_0(self, tmp_path, monkeypatch):
        _run_main(monkeypatch, tmp_path, "--mock")
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_mock_failure_exits_1(self, tmp_path, monkeypatch):
        dag_file = _make_failing_dag_file(tmp_path)
        monkeypatch.setattr(sys, "argv", ["polyris-local", "--file", dag_file, "--mock"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_asl_flag_shows_asl(self, tmp_path, monkeypatch, capsys):
        _run_main(monkeypatch, tmp_path, "--asl")
        with pytest.raises(SystemExit):
            main()
        out = capsys.readouterr().out
        assert '"StartAt"' in out

    def test_file_flag_loads_named_file(self, tmp_path, monkeypatch):
        """--file is already used by _run_main above; this confirms the flag
        name itself (not just the default) routes correctly."""
        dag_file = _make_dag_file(tmp_path, arns_real=True)
        monkeypatch.setattr(sys, "argv", ["polyris-local", "--file", dag_file, "--validate"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_missing_file_exits_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["polyris-local", "--file", str(tmp_path / "missing.py")])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1
