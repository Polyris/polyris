"""
Tests for evaluate_deps Lambda

Coverage:
- All 11 trigger_rules
- Edge cases: empty deps, all pending, mixed statuses
- Pause handling
- Error scenarios
"""

# pytest-mock: mocker fixture used for patching
import sys
import os

# Add lambda to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluate_deps.index import (
    handler,
    _check_trigger_rule,
    _calculate_counts,
    TERMINAL_SUCCESS,
    TERMINAL_FAILURE,
    TERMINAL_STATUSES,
)
from evaluate_deps.dal import TokensRepo


class TestCheckTriggerRule:
    """Test trigger_rule evaluation logic."""
    
    # ===== all_success (default) =====
    
    def test_all_success_with_all_success(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'success'])
        assert is_ready is True
        assert reason == 'all_success'
    
    def test_all_success_with_success_and_skipped(self):
        """Skipped counts as OK for all_success when origin is unknown (default)."""
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'skipped'])
        assert is_ready is True
        assert reason == 'all_success'

    def test_all_success_rule_originated_skip_blocks(self):
        """ADR #115 step 1.2: rule_originated_skip=[False, True] means the second
        dep's skip is rule-originated -> cascades, all_success is NOT satisfied."""
        is_ready, reason, _ = _check_trigger_rule(
            'all_success', ['success', 'skipped'], rule_originated_skip=[False, True]
        )
        assert is_ready is False
        assert 'rule-skipped' in reason

    def test_all_success_non_rule_skip_with_explicit_false_still_ok(self):
        """rule_originated_skip=[False, False] (explicitly known non-rule) behaves
        the same as not passing it at all."""
        is_ready, _, _ = _check_trigger_rule(
            'all_success', ['success', 'skipped'], rule_originated_skip=[False, False]
        )
        assert is_ready is True

    def test_all_success_mixed_rule_and_manual_skip(self):
        """One rule-originated skip among several is enough to block all_success,
        even if the other skip is not rule-originated."""
        is_ready, _, _ = _check_trigger_rule(
            'all_success', ['success', 'skipped', 'skipped'],
            rule_originated_skip=[False, False, True],
        )
        assert is_ready is False

    def test_all_success_rule_originated_skip_does_not_affect_other_rules(self):
        """rule_originated_skip is only consulted by all_success; every other rule
        ignores it entirely (skipped counts toward its own dedicated bucket, as
        before)."""
        is_ready, _, _ = _check_trigger_rule(
            'none_skipped', ['success', 'skipped'], rule_originated_skip=[False, True]
        )
        assert is_ready is False  # blocked by none_skipped's own logic (a skip exists),
        # not reinterpreted via rule_originated_skip
    
    def test_all_success_with_failure(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'failed'])
        assert is_ready is False
        assert 'failed' in reason
    
    def test_all_success_with_pending(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'waiting'])
        assert is_ready is False
        assert 'waiting' in reason
    
    def test_all_success_with_upstream_failed(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'upstream_failed'])
        assert is_ready is False
        assert 'failed' in reason
    
    def test_all_success_with_aborted(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'aborted'])
        assert is_ready is False
        assert 'failed' in reason
    
    # ===== one_success =====
    
    def test_one_success_with_one(self):
        """Triggers immediately when one succeeds - doesn't wait!"""
        is_ready, reason, _ = _check_trigger_rule('one_success', ['success', 'waiting'])
        assert is_ready is True
        assert reason == 'one_success'
    
    def test_one_success_none_yet(self):
        is_ready, reason, _ = _check_trigger_rule('one_success', ['waiting', 'running'])
        assert is_ready is False
        assert 'waiting' in reason
    
    def test_one_success_all_failed(self):
        is_ready, reason, _ = _check_trigger_rule('one_success', ['failed', 'failed'])
        assert is_ready is False
        assert 'none succeeded' in reason
    
    # ===== all_done =====
    
    def test_all_done_mixed_terminal(self):
        is_ready, reason, _ = _check_trigger_rule('all_done', ['success', 'failed', 'skipped'])
        assert is_ready is True
        assert reason == 'all_done'
    
    def test_all_done_with_pending(self):
        is_ready, reason, _ = _check_trigger_rule('all_done', ['success', 'waiting'])
        assert is_ready is False
        assert 'waiting' in reason
    
    # ===== all_skipped =====
    
    def test_all_skipped_with_all(self):
        is_ready, reason, _ = _check_trigger_rule('all_skipped', ['skipped', 'skipped'])
        assert is_ready is True
        assert reason == 'all_skipped'
    
    def test_all_skipped_with_success(self):
        is_ready, reason, _ = _check_trigger_rule('all_skipped', ['skipped', 'success'])
        assert is_ready is False
    
    # ===== none_skipped =====
    
    def test_none_skipped_all_success(self):
        is_ready, reason, _ = _check_trigger_rule('none_skipped', ['success', 'success'])
        assert is_ready is True
        assert reason == 'none_skipped'
    
    def test_none_skipped_with_skip(self):
        is_ready, reason, _ = _check_trigger_rule('none_skipped', ['success', 'skipped'])
        assert is_ready is False
        assert 'skipped' in reason
    
    # ===== Edge cases =====
    
    def test_empty_deps(self):
        is_ready, reason, _ = _check_trigger_rule('all_success', [])
        assert is_ready is True
        assert reason == 'no_deps'
    
    def test_unknown_trigger_rule_defaults_to_all_success(self):
        """Unknown rules fall back to all_success."""
        is_ready, reason, _ = _check_trigger_rule('invalid_rule', ['success', 'success'])
        assert is_ready is True
    
    def test_not_found_counts_as_pending(self):
        """not_found status means task hasn't registered yet."""
        is_ready, reason, _ = _check_trigger_rule('all_success', ['success', 'not_found'])
        assert is_ready is False
        assert 'waiting' in reason


class TestCalculateCounts:
    """Test status counting logic."""
    
    def test_all_success(self):
        counts = _calculate_counts(['success', 'success', 'success'])
        assert counts == {'total': 3, 'success': 3, 'failed': 0, 'skipped': 0, 'pending': 0}
    
    def test_mixed_statuses(self):
        counts = _calculate_counts(['success', 'failed', 'skipped', 'waiting'])
        assert counts == {'total': 4, 'success': 1, 'failed': 1, 'skipped': 1, 'pending': 1}
    
    def test_failure_types(self):
        """failed, upstream_failed, aborted all count as failed."""
        counts = _calculate_counts(['failed', 'upstream_failed', 'aborted'])
        assert counts['failed'] == 3
        assert counts['pending'] == 0
    
    def test_pending_types(self):
        """waiting, running, deps_ready, not_found all count as pending."""
        counts = _calculate_counts(['waiting', 'running', 'deps_ready', 'not_found', 'waiting_delay'])
        assert counts['pending'] == 5
        assert counts['success'] == 0
    
    def test_empty(self):
        counts = _calculate_counts([])
        assert counts == {'total': 0, 'success': 0, 'failed': 0, 'skipped': 0, 'pending': 0}



class TestHandler:
    """Test main handler function.

    v0.79.3 (ADR #75) — tests mock `tokens_repo.batch_get_statuses` /
    `.is_paused` directly instead of patching boto3 internals. The DAL
    pattern makes test setup ~10x shorter.
    """

    def test_empty_dependencies(self, mocker):
        """Empty deps = immediately ready."""
        # No DDB access expected; DAL methods don't need patching.
        result = handler({'dependencies': [], 'trigger_rule': 'all_success'}, None)
        assert result['is_ready'] is True
        assert result['is_blocked'] is False
        assert result['reason'] == 'no_deps'
        assert result['dep_statuses'] == []

    def test_all_ready(self, mocker):
        """All deps success = ready."""
        mocker.patch('evaluate_deps.index.tokens_repo.batch_get_statuses',
                     return_value={
                         'task_a-2026-01-19-abc123': 'success',
                         'task_b-2026-01-19-abc123': 'success',
                     })
        result = handler({
            'dependencies': ['task_a', 'task_b'],
            'trigger_rule': 'all_success',
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': '',
        }, None)
        assert result['is_ready'] is True
        assert result['is_blocked'] is False
        assert result['dep_statuses'] == ['success', 'success']
        assert result['counts']['success'] == 2

    def test_blocked_scenario(self, mocker):
        """All done but rule not satisfied = blocked."""
        mocker.patch('evaluate_deps.index.tokens_repo.batch_get_statuses',
                     return_value={
                         'task_a-2026-01-19-abc123': 'success',
                         'task_b-2026-01-19-abc123': 'failed',
                     })
        result = handler({
            'dependencies': ['task_a', 'task_b'],
            'trigger_rule': 'all_success',
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': '',
        }, None)
        assert result['is_ready'] is False
        assert result['is_blocked'] is True
        assert result['counts']['failed'] == 1

    def test_pending_scenario(self, mocker):
        """Some still running = pending (waiting)."""
        mocker.patch('evaluate_deps.index.tokens_repo.batch_get_statuses',
                     return_value={
                         'task_a-2026-01-19-abc123': 'success',
                         'task_b-2026-01-19-abc123': 'running',
                     })
        result = handler({
            'dependencies': ['task_a', 'task_b'],
            'trigger_rule': 'all_success',
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': '',
        }, None)
        assert result['is_ready'] is False
        assert result['is_blocked'] is False
        assert result['counts']['pending'] == 1

    def test_paused_pipeline(self, mocker):
        """Paused pipeline = not ready even if deps are ready, but deps_satisfied=True."""
        mocker.patch('evaluate_deps.index.tokens_repo.batch_get_statuses',
                     return_value={'task_a-2026-01-19-abc123': 'success'})
        mocker.patch('evaluate_deps.index.tokens_repo.is_paused',
                     return_value=True)
        result = handler({
            'dependencies': ['task_a'],
            'trigger_rule': 'all_success',
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': 'pipeline-2026-01-19-abc123',
        }, None)
        assert result['is_ready'] is False
        assert result['is_paused'] is True
        assert result['deps_satisfied'] is True

    def test_paused_pipeline_deps_not_ready(self, mocker):
        """Paused + deps not ready = is_ready=False, deps_satisfied=False."""
        mocker.patch('evaluate_deps.index.tokens_repo.batch_get_statuses',
                     return_value={'task_a-2026-01-19-abc123': 'waiting'})
        mocker.patch('evaluate_deps.index.tokens_repo.is_paused',
                     return_value=True)
        result = handler({
            'dependencies': ['task_a'],
            'trigger_rule': 'all_success',
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': 'pipeline-2026-01-19-abc123',
        }, None)
        assert result['is_ready'] is False
        assert result['is_paused'] is True
        assert result['deps_satisfied'] is False


class TestStatusCategories:
    """Verify status category definitions.

    v0.79.5 (ADR #77) — sets are now imported from canonical
    polyris/constants.py via generated module-level constants. The
    canonical TaskStatus enum includes both 'success' (legacy form) and
    'succeeded' (SFN-normalized form), so the terminal/success sets
    contain both.
    """

    def test_terminal_success(self):
        # Both legacy 'success' and SFN-normalized 'succeeded' are success-like
        assert TERMINAL_SUCCESS == {'success', 'succeeded', 'skipped'}

    def test_terminal_failure(self):
        assert TERMINAL_FAILURE == {'failed', 'upstream_failed', 'aborted'}

    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == {
            'success', 'succeeded', 'skipped',
            'failed', 'upstream_failed', 'aborted',
        }


class TestVerdict:
    """Test the 'verdict' field (ADR #115): ready | wait | skip | upstream_failed.

    A blocked rule (all deps terminal, rule not satisfied) is 'upstream_failed' only
    when the rule requires success/no-failure (FAILURE_AVERSE_RULES) AND a real
    failure is present among the deps. Otherwise it is 'skip' — the rule's trigger
    condition simply never occurred, which is a legitimate no-op, not an error.
    """

    def _call(self, mocker, trigger_rule, statuses, dep_names=None, skip_origins=None):
        dep_names = dep_names or [f"dep_{i}" for i in range(len(statuses))]
        mocker.patch(
            'evaluate_deps.index.tokens_repo.batch_get_statuses',
            return_value={
                f"{name}-2026-01-19-abc123": status
                for name, status in zip(dep_names, statuses)
            },
        )
        # ADR #115 step 1.2: sparse by design — absence means "no rule-originated
        # skip known", i.e. every skip is treated as non-cascading unless the test
        # explicitly supplies an origin via skip_origins={name: 'rule'}.
        mocker.patch(
            'evaluate_deps.index.tokens_repo.batch_get_skip_origins',
            return_value={
                f"{name}-2026-01-19-abc123": origin
                for name, origin in (skip_origins or {}).items()
            },
        )
        return handler({
            'dependencies': dep_names,
            'trigger_rule': trigger_rule,
            'date': '2026-01-19',
            'pipeline_execution_short': 'abc123',
            'pipeline_execution': '',
        }, None)

    # ===== ready / wait (unaffected by ADR #115 — sanity checks) =====

    def test_ready_when_satisfied(self, mocker):
        result = self._call(mocker, 'all_success', ['success', 'success'])
        assert result['verdict'] == 'ready'

    def test_wait_when_pending(self, mocker):
        result = self._call(mocker, 'all_success', ['success', 'running'])
        assert result['verdict'] == 'wait'
        assert result['is_blocked'] is False

    # ===== skip: the originally-observed bug scenarios =====

    def test_all_skipped_not_all_skipped_is_skip(self, mocker):
        """trigger-rules-reference: both extractors succeed (not skipped).
        all_skipped's condition never occurs — 'skip', not a failure."""
        result = self._call(mocker, 'all_skipped', ['success', 'success'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'skip'

    def test_none_skipped_blocked_by_a_skip_is_skip(self, mocker):
        """A skip (not a failure) caused the block — 'skip', never upstream_failed."""
        result = self._call(mocker, 'none_skipped', ['success', 'skipped'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'skip'

    # ===== skip: FAILURE_AVERSE_RULES blocked by a skip only (no real failure) =====

    def test_all_success_unknown_origin_skip_is_still_ok(self, mocker):
        """ADR #115 step 1.2: a skip with NO known origin (skip_origins not supplied
        — matches a manual skip, or any pre-existing/legacy skipped row) still
        counts as 'ok' for all_success. Only an explicitly rule-originated skip
        cascades (see test_all_success_rule_originated_skip_cascades below)."""
        result = self._call(mocker, 'all_success', ['success', 'skipped'])
        assert result['verdict'] == 'ready'

    def test_all_success_manual_skip_does_not_cascade(self, mocker):
        """A skip explicitly tagged skip_origin='manual' behaves identically to an
        unknown origin — still 'ok', does not cascade."""
        result = self._call(
            mocker, 'all_success', ['success', 'skipped'],
            dep_names=['dep_a', 'dep_b'], skip_origins={'dep_b': 'manual'},
        )
        assert result['verdict'] == 'ready'

    def test_all_success_rule_originated_skip_cascades(self, mocker):
        """ADR #115 step 1.2: a skip tagged skip_origin='rule' (the wrapper's
        Auto_Skip path, from a trigger_rule condition that never occurred) DOES
        cascade — all_success now blocks and resolves 'skipped' downstream, not
        'ready'. This is the actual behavior change this step delivers."""
        result = self._call(
            mocker, 'all_success', ['success', 'skipped'],
            dep_names=['dep_a', 'dep_b'], skip_origins={'dep_b': 'rule'},
        )
        assert result['is_blocked'] is True
        assert result['verdict'] == 'skip'

    def test_all_success_all_rule_skipped_cascades_to_skip(self, mocker):
        """Every upstream rule-skipped -> all_success cascades to skip, not ready."""
        result = self._call(
            mocker, 'all_success', ['skipped', 'skipped'],
            dep_names=['dep_a', 'dep_b'],
            skip_origins={'dep_a': 'rule', 'dep_b': 'rule'},
        )
        assert result['verdict'] == 'skip'

    def test_one_success_blocked_all_skipped_is_skip(self, mocker):
        result = self._call(mocker, 'one_success', ['skipped', 'skipped'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'skip'

    # ===== upstream_failed: FAILURE_AVERSE_RULES blocked by a real failure =====

    def test_all_success_blocked_by_failure_is_upstream_failed(self, mocker):
        """Unchanged from today: a genuine failure blocking all_success is a real
        upstream problem."""
        result = self._call(mocker, 'all_success', ['success', 'failed'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'upstream_failed'

    def test_one_success_blocked_by_failure_only_is_upstream_failed(self, mocker):
        """Zero successes and a real failure present (not just skips) — a genuine
        problem, not a no-op."""
        result = self._call(mocker, 'one_success', ['failed', 'failed'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'upstream_failed'

    # ===== rules that never legitimately signal upstream_failed =====

    def test_all_done_never_blocks(self, mocker):
        """all_done is satisfied whenever pending == 0 regardless of outcome, so it
        structurally cannot reach is_blocked=True."""
        result = self._call(mocker, 'all_done', ['failed', 'skipped', 'success'])
        assert result['is_blocked'] is False
        assert result['verdict'] == 'ready'

    # ===== unknown rule falls back to all_success's effective_rule classification =====

    def test_unknown_rule_blocked_by_failure_classifies_as_all_success(self, mocker):
        """An unrecognized trigger_rule silently evaluates as all_success (existing
        behavior); the verdict classification must follow the *effective* rule that
        was actually evaluated, not the raw unrecognized string — otherwise this
        case would default to 'skip' (unrecognized string not in
        FAILURE_AVERSE_RULES) instead of the correct 'upstream_failed'."""
        result = self._call(mocker, 'not_a_real_rule', ['success', 'failed'])
        assert result['is_blocked'] is True
        assert result['verdict'] == 'upstream_failed'

    def test_empty_deps_verdict_is_ready(self):
        result = handler({'dependencies': [], 'trigger_rule': 'all_success'}, None)
        assert result['verdict'] == 'ready'


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_trigger_rule_one_success_doesnt_wait(self):
        """Critical: one_success triggers immediately, doesn't wait for all deps."""
        # One success, others still running - should be ready!
        is_ready, _, _ = _check_trigger_rule('one_success', ['success', 'running', 'waiting'])
        assert is_ready is True
    
    def test_skipped_treated_as_ok_for_all_success(self):
        """Manual skip via UI should allow pipeline to continue."""
        is_ready, _, _ = _check_trigger_rule('all_success', ['success', 'skipped', 'skipped'])
        assert is_ready is True
    
    def test_waiting_decision_counts_as_pending(self):
        """waiting_decision status = task waiting for human decision."""
        counts = _calculate_counts(['waiting_decision'])
        assert counts['pending'] == 1
    
    def test_waiting_paused_counts_as_pending(self):
        counts = _calculate_counts(['waiting_paused'])
        assert counts['pending'] == 1


class TestAssetsReadyCoordination:
    """Case B (Option J): evaluate_deps is the single gate that considers BOTH
    task_deps AND wait_for assets. When a task has wait_for, verdict='ready'
    requires assets_ready=true — otherwise it returns 'wait', letting whichever
    signal path (notify_dependents or notify_asset_subscribers) fires *last*
    be the one to signal wait_token."""

    def test_no_wait_for_ignores_assets_ready_flag(self, mocker):
        """Baseline: a task without wait_for behaves exactly as before —
        assets_ready is ignored."""
        mocker.patch("evaluate_deps.index._batch_get_dep_statuses",
                     return_value=["success", "success"])
        mocker.patch("evaluate_deps.index._is_pipeline_paused", return_value=False)
        result = handler({
            "dependencies": ["a", "b"],
            "trigger_rule": "all_success",
        }, None)
        assert result["is_ready"] is True
        assert result["verdict"] == "ready"
        assert result.get("assets_satisfied") is True

    def test_wait_for_present_and_assets_ready_false_returns_wait(self, mocker):
        """Task_deps satisfy the rule BUT wait_for assets aren't ready — the
        verdict must be 'wait', not 'ready'. Prevents dep_wrapper from waking
        with pending assets."""
        mocker.patch("evaluate_deps.index._batch_get_dep_statuses",
                     return_value=["success", "success"])
        mocker.patch("evaluate_deps.index._is_pipeline_paused", return_value=False)
        result = handler({
            "dependencies": ["a", "b"],
            "trigger_rule": "all_success",
            "wait_for": [{"asset_name": "inventory"}],
            "assets_ready": False,
        }, None)
        assert result["is_ready"] is False
        assert result["verdict"] == "wait"
        assert result["deps_satisfied"] is True
        assert result["assets_satisfied"] is False

    def test_wait_for_present_and_assets_ready_true_returns_ready(self, mocker):
        """Both task_deps AND assets ready → verdict='ready', is_ready=True.
        This is the case notify_asset_subscribers hits after flipping the
        flag when task_deps were already done."""
        mocker.patch("evaluate_deps.index._batch_get_dep_statuses",
                     return_value=["success", "success"])
        mocker.patch("evaluate_deps.index._is_pipeline_paused", return_value=False)
        result = handler({
            "dependencies": ["a", "b"],
            "trigger_rule": "all_success",
            "wait_for": [{"asset_name": "inventory"}],
            "assets_ready": True,
        }, None)
        assert result["is_ready"] is True
        assert result["verdict"] == "ready"

    def test_no_deps_wait_for_present_assets_not_ready(self, mocker):
        """Task with only wait_for (no task_deps) and assets not ready → wait.
        Guards the empty-dependencies fast-path from the same class of bug."""
        mocker.patch("evaluate_deps.index._is_pipeline_paused", return_value=False)
        result = handler({
            "dependencies": [],
            "trigger_rule": "all_success",
            "wait_for": [{"asset_name": "inventory"}],
            "assets_ready": False,
        }, None)
        assert result["is_ready"] is False
        assert result["verdict"] == "wait"
        assert result["reason"] == "awaiting_assets"

    def test_skip_verdict_unaffected_by_assets_ready(self, mocker):
        """Case A analog: a task whose task_deps produce verdict='skip' is
        resolved regardless of assets — it can never succeed anyway. The
        registration SFN's Route_Combined_Check already routes on this;
        here we pin that evaluate_deps also doesn't require assets_ready to
        report skip."""
        mocker.patch("evaluate_deps.index._batch_get_dep_statuses",
                     return_value=["skipped", "skipped"])
        mocker.patch("evaluate_deps.index._is_pipeline_paused", return_value=False)
        mocker.patch("evaluate_deps.index._batch_get_rule_originated_skip",
                     return_value=None)
        result = handler({
            "dependencies": ["a", "b"],
            "trigger_rule": "none_skipped",
            "wait_for": [{"asset_name": "inventory"}],
            "assets_ready": False,
        }, None)
        assert result["verdict"] == "skip"


def test_removed_rule_aliases_map_to_canonical_equivalents():
    """ADR #117 removed 6 redundant trigger rules. Pipelines deployed before the
    trim carry the old rule names in their SFN input, so evaluate_deps must map
    them to their canonical equivalents at runtime rather than silently falling
    back to all_success (which has different semantics for cleanup patterns).

    Concrete risk: none_failed was used for cleanup tasks expected to always run
    after deps finish. Falling back to all_success marks such a task upstream_failed
    when any dep is failed — exactly the wrong behaviour for a cleanup task."""
    # none_failed ≡ all_done: fires when all deps are terminal, regardless of status
    assert _check_trigger_rule("none_failed", ["success", "failed"])[0] is True
    assert _check_trigger_rule("none_failed", ["success", "skipped"])[0] is True
    assert _check_trigger_rule("none_failed", ["failed", "failed"])[0] is True
    assert _check_trigger_rule("none_failed", ["success", "waiting"])[0] is False  # pending

    # one_done ≡ all_done (same semantics, rejected alias name)
    assert _check_trigger_rule("one_done", ["success", "failed"])[0] is True
    assert _check_trigger_rule("one_done", ["skipped", "waiting"])[0] is False  # pending

    # none_failed_min_one_success ≡ one_success: fires as soon as one dep succeeds
    assert _check_trigger_rule("none_failed_min_one_success", ["success", "failed"])[0] is True
    assert _check_trigger_rule("none_failed_min_one_success", ["skipped", "skipped"])[0] is False
    assert _check_trigger_rule("none_failed_min_one_success", ["success", "waiting"])[0] is True  # one_success fires immediately



def test_succeeded_alias_satisfies_success_rules():
    """Regression: 'succeeded' is the legacy alias for success and is what
    normalize_execution_status() produces from AWS SFN's 'SUCCEEDED'.
    _calculate_counts once counted only the literal 'success', so any dependency
    reporting 'succeeded' left all_success (the DEFAULT rule) unsatisfied forever —
    a silent pipeline deadlock. All success-oriented rules must treat the two
    aliases identically."""
    assert _check_trigger_rule("all_success", ["succeeded", "succeeded"])[0] is True
    assert _check_trigger_rule("all_success", ["success", "succeeded"])[0] is True
    assert _check_trigger_rule("all_success", ["succeeded", "skipped"])[0] is True
    assert _check_trigger_rule("all_success", ["succeeded", "failed"])[0] is False
    assert _check_trigger_rule("one_success", ["failed", "succeeded"])[0] is True
    assert _check_trigger_rule("all_done", ["succeeded", "failed"])[0] is True
    # 'succeeded' must NOT count as skipped
    assert _check_trigger_rule("all_skipped", ["succeeded", "skipped"])[0] is False


class TestAbsorbSkipOriginResponse:
    """Direct tests for TokensRepo._absorb_skip_origin_response (ADR #115 step 1.2).

    Pure/dependency-free (no boto3 client needed) — matches the sibling
    _absorb_batch_response, which is likewise only covered indirectly elsewhere;
    this one gets a direct test since its sparse-inclusion logic (skip an item
    entirely when skip_origin is absent, rather than defaulting to some sentinel)
    is easy to get subtly wrong.
    """

    def test_present_skip_origin_is_absorbed(self):
        results = {}
        response = {
            'Responses': {
                'tokens': [
                    {'execution_name': {'S': 'dep-2026-01-19-abc'}, 'skip_origin': {'S': 'rule'}},
                ]
            }
        }
        TokensRepo._absorb_skip_origin_response(response, 'tokens', results)
        assert results == {'dep-2026-01-19-abc': 'rule'}

    def test_missing_skip_origin_attribute_is_not_added(self):
        """An item with no skip_origin attribute at all must NOT appear in results
        (sparse by design) - not added as None or '' or any other sentinel."""
        results = {}
        response = {
            'Responses': {
                'tokens': [
                    {'execution_name': {'S': 'dep-2026-01-19-abc'}},
                ]
            }
        }
        TokensRepo._absorb_skip_origin_response(response, 'tokens', results)
        assert results == {}

    def test_missing_execution_name_is_not_added(self):
        results = {}
        response = {'Responses': {'tokens': [{'skip_origin': {'S': 'rule'}}]}}
        TokensRepo._absorb_skip_origin_response(response, 'tokens', results)
        assert results == {}

    def test_empty_response_leaves_results_untouched(self):
        results = {'pre-existing': 'rule'}
        TokensRepo._absorb_skip_origin_response({}, 'tokens', results)
        assert results == {'pre-existing': 'rule'}

    def test_multiple_items_mixed_presence(self):
        results = {}
        response = {
            'Responses': {
                'tokens': [
                    {'execution_name': {'S': 'a'}, 'skip_origin': {'S': 'manual'}},
                    {'execution_name': {'S': 'b'}},  # no skip_origin
                    {'execution_name': {'S': 'c'}, 'skip_origin': {'S': 'rule'}},
                ]
            }
        }
        TokensRepo._absorb_skip_origin_response(response, 'tokens', results)
        assert results == {'a': 'manual', 'c': 'rule'}
