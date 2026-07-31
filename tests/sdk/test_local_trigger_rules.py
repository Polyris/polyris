"""
Tests for polyris.local's _evaluate_trigger_rule (the mock-execution runner's
trigger-rule evaluator used by `run(dag, mock=True)`).

Context: this function previously implemented only 7 of the then-11 documented
trigger rules (docs/features/DSL.md#trigger-rules); for `all_done_min_one_success`,
`one_done`, `all_skipped`, and `none_skipped` it silently fell through to
`return True, "default"` — meaning a mock test using any of those four rules
would always let the task run, regardless of the actual upstream outcomes,
giving false confidence. This file exercises all rules directly so the fix
has real coverage (the function is pure logic with zero AWS dependency, even
though `polyris/local.py` as a whole is in the coverage `omit` list for its
other, AWS-calling parts — see pyproject.toml's `[tool.coverage.run]` comment).

ADR #117 later trimmed the accepted rule set from 11 to 5 (`all_success`,
`one_success`, `all_done`, `all_skipped`, `none_skipped`); this file now only
covers those, plus the unknown-rule fallback (which also catches any of the
6 removed names, alongside a genuine typo).

Uses lightweight fakes (SimpleNamespace) rather than constructing real Task/DAG
objects, since `_evaluate_trigger_rule` only reads `task.trigger_rule` and
`t.node_id` for each of `task.dependencies` — no DAG context is needed to test
it in isolation.
"""
from types import SimpleNamespace
from datetime import datetime, timezone

from polyris.local import _evaluate_trigger_rule, TaskResult


def _dep(node_id):
    return SimpleNamespace(node_id=node_id)


def _task(trigger_rule, dep_ids):
    return SimpleNamespace(trigger_rule=trigger_rule, dependencies=[_dep(d) for d in dep_ids])


def _result(task_id, status):
    now = datetime.now(timezone.utc)
    return TaskResult(task_id=task_id, status=status, start_time=now, end_time=now)


class TestNoDependencies:
    def test_no_dependencies_always_runs(self):
        task = _task('all_success', [])
        should_run, reason = _evaluate_trigger_rule(task, [])
        assert should_run is True
        assert reason == "no upstream dependencies"


class TestAllSuccess:
    def test_all_succeeded(self):
        task = _task('all_success', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'success')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is True

    def test_one_failed_blocks(self):
        task = _task('all_success', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'failed')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is False

    def test_one_skipped_blocks(self):
        """No skip_origin concept in this mock model (ADR #115 doesn't apply
        here) — any skip blocks all_success, matching the cascade intent."""
        task = _task('all_success', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'skipped')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is False


class TestAllDone:
    def test_all_terminal_any_mix(self):
        task = _task('all_done', ['a', 'b', 'c'])
        results = [_result('a', 'success'), _result('b', 'failed'), _result('c', 'skipped')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is True


class TestAllSkipped:
    """Previously always returned True regardless of input — the core bug."""

    def test_all_skipped_runs(self):
        task = _task('all_skipped', ['a', 'b'])
        results = [_result('a', 'skipped'), _result('b', 'skipped')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is True
        assert reason == "all_skipped"

    def test_not_all_skipped_does_not_run(self):
        """The regression case: before the fix, this incorrectly ran."""
        task = _task('all_skipped', ['a', 'b'])
        results = [_result('a', 'skipped'), _result('b', 'success')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert "not all skipped" in reason


class TestOneSuccess:
    def test_one_success_is_enough(self):
        task = _task('one_success', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'failed')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is True

    def test_zero_success_does_not_run(self):
        task = _task('one_success', ['a', 'b'])
        results = [_result('a', 'failed'), _result('b', 'skipped')]
        should_run, _ = _evaluate_trigger_rule(task, results)
        assert should_run is False


class TestNoneSkipped:
    """Previously always returned True regardless of input — the core bug."""

    def test_no_skips_runs(self):
        task = _task('none_skipped', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'failed')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is True
        assert reason == "none_skipped"

    def test_any_skip_blocks(self):
        """The regression case: before the fix, this incorrectly ran."""
        task = _task('none_skipped', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'skipped')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert "1 skipped" in reason


class TestUnknownRule:
    """An unrecognized trigger_rule string (including any of ADR #117's 6
    removed names) defaults to all_success semantics (ADR #115 consistency
    with evaluate_deps), not an unconditional True."""

    def test_unknown_rule_all_succeeded_runs(self):
        task = _task('not_a_real_rule', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'success')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is True
        assert 'defaulted_to_all_success' in reason

    def test_unknown_rule_not_all_succeeded_does_not_run(self):
        task = _task('not_a_real_rule', ['a', 'b'])
        results = [_result('a', 'success'), _result('b', 'failed')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert 'not_a_real_rule' in reason

    def test_removed_rule_all_failed_defaults_to_all_success(self):
        """A rule name from ADR #117's removed set is, to this function,
        just another unrecognized string — it hits the same fallback as a
        typo. validate_asl_from_dag is what gives the user a specific,
        helpful message; this is a defensive fallback, not the primary path."""
        task = _task('all_failed', ['a', 'b'])
        results = [_result('a', 'failed'), _result('b', 'failed')]
        should_run, reason = _evaluate_trigger_rule(task, results)
        assert should_run is False
        assert 'defaulted to all_success' in reason
