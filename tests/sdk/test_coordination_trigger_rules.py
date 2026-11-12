"""Trigger rules over dependency statuses (ADR #115, #117) — the shared module."""

import pytest

from polyris.coordination import trigger_rules as tr


S, F, K, W = "success", "failed", "skipped", "waiting"


class TestCount:
    def test_counts_every_category(self):
        c = tr.count([S, "succeeded", F, "aborted", "upstream_failed", K, W, "not_found"])
        assert (c.total, c.success, c.failed, c.skipped, c.pending) == (8, 2, 3, 1, 2)


class TestVerdicts:
    @pytest.mark.parametrize("rule,statuses,verdict", [
        # all_success
        ("all_success", [S, S], tr.READY),
        ("all_success", [S, "succeeded"], tr.READY),   # legacy alias counts as success
        ("all_success", [S, K], tr.READY),              # a plain skip counts as ok
        ("all_success", [S, W], tr.WAIT),
        ("all_success", [S, F], tr.UPSTREAM_FAILED),
        # one_success: fires as soon as one dependency succeeded
        ("one_success", [S, W], tr.READY),
        ("one_success", [W, F], tr.WAIT),
        ("one_success", [F, F], tr.UPSTREAM_FAILED),
        ("one_success", [K, K], tr.SKIP),
        # all_done
        ("all_done", [S, F], tr.READY),
        ("all_done", [S, W], tr.WAIT),
        # all_skipped
        ("all_skipped", [K, K], tr.READY),
        ("all_skipped", [K, W], tr.WAIT),
        ("all_skipped", [K, S], tr.SKIP),
        # none_skipped
        ("none_skipped", [S, S], tr.READY),
        ("none_skipped", [S, W], tr.WAIT),
        ("none_skipped", [S, K], tr.SKIP),
    ])
    def test_rule_table(self, rule, statuses, verdict):
        assert tr.evaluate(rule, statuses).verdict == verdict

    def test_no_dependencies_is_ready(self):
        result = tr.evaluate("all_success", [])
        assert (result.verdict, result.satisfied, result.reason) == (tr.READY, True, "no dependencies")

    def test_rule_originated_skip_cascades_under_all_success(self):
        result = tr.evaluate("all_success", [S, K], rule_originated_skip=[False, True])
        assert result.verdict == tr.SKIP
        assert "skipped by their own rule" in result.reason

    def test_rule_originated_skip_length_must_match(self):
        with pytest.raises(ValueError):
            tr.evaluate("all_success", [S, K], rule_originated_skip=[True])


class TestAliasesAndUnknownRules:
    @pytest.mark.parametrize("alias,canonical", sorted(tr.ALIASES.items()))
    def test_alias_evaluates_as_its_canonical_rule(self, alias, canonical):
        result = tr.evaluate(alias, [S, F])
        assert result.effective_rule == canonical
        assert result.rule_known is True
        assert result.verdict == tr.evaluate(canonical, [S, F]).verdict

    def test_none_failed_min_one_success_blocked_by_failure_is_upstream_failed(self):
        # The alias behaves as one_success (ADR #117), a failure-averse rule.
        # The removed evaluate_deps Lambda kept the alias name and classified this as skip.
        assert tr.evaluate("none_failed_min_one_success", [F, F]).verdict == tr.UPSTREAM_FAILED

    def test_unknown_rule_is_evaluated_as_all_success_and_flagged(self):
        result = tr.evaluate("one_failed", [S, F])
        assert result.rule_known is False
        assert result.effective_rule == "all_success"
        assert result.verdict == tr.UPSTREAM_FAILED
        assert "unknown trigger_rule 'one_failed'" in result.reason
