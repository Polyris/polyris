"""extract_pipeline_execution_short — its three-tier fallback logic had no direct test
anywhere in either repo. `task_actions.py` itself had no test file at all; the only
references were mocks in EE's Slack-action tests (`test_slack_actions.py`), which
isolate the Slack orchestration, not this function's own fallback correctness.

Found auditing this session's changes against the rest of the codebase (Principle #14):
mocking it there is a legitimate isolation of a *different* surface, but that leaves
this function's own three-tier priority never exercised for real — the exact gap #14
warns produces "a green suite where the actual bug hides". No boundary here to mock;
it is pure string logic over a dict and a name.
"""
from task_actions import extract_pipeline_execution_short


class TestExtractPipelineExecutionShort:
    def test_first_priority_is_the_stored_value(self):
        item = {'pipeline_execution_short': 'stored123', 'pipeline_execution': 'ignored'}
        assert extract_pipeline_execution_short(item, 'extract-2026-07-16-ignored') == 'stored123'

    def test_second_priority_computes_from_pipeline_execution(self):
        # compute_pipeline_execution_short: last 20 chars, strip '.' and ':'
        item = {'pipeline_execution': 'sales-hourly-2026-07-16-abc123def456'}
        expected_full = item['pipeline_execution'][-20:].replace('.', '').replace(':', '')
        assert extract_pipeline_execution_short(item, 'irrelevant') == expected_full

    def test_third_priority_falls_back_to_the_execution_name_suffix(self):
        item = {}      # no stored value, no pipeline_execution
        assert extract_pipeline_execution_short(item, 'extract-2026-07-16-abc123') == 'abc123'

    def test_execution_name_with_too_few_hyphens_yields_nothing(self):
        """The suffix fallback only fires on the task_name-YYYY-MM-DD-short_id shape
        (>= 3 hyphens); anything shorter has no reliable short id to extract."""
        item = {}
        assert extract_pipeline_execution_short(item, 'ab-cd') == ''

    def test_empty_stored_value_falls_through_rather_than_short_circuiting(self):
        """An empty string is falsy, same as absent — must not be returned as-is."""
        item = {'pipeline_execution_short': '', 'pipeline_execution': 'p-2026-07-16-xyz'}
        assert extract_pipeline_execution_short(item, 'irrelevant') != ''

    def test_priority_order_is_stored_then_computed_then_suffix(self):
        """All three sources present at once — the stored value must win."""
        item = {'pipeline_execution_short': 'winner', 'pipeline_execution': 'p-loses-too'}
        assert extract_pipeline_execution_short(item, 'extract-2026-07-16-loses') == 'winner'


class TestTerminalConditionExpression:
    """Regression tests for a real drift found in a code-review pass:
    TERMINAL_CONDITION_EXPRESSION and build_condition_expression_values were
    a hand-maintained duplicate of the canonical TASK_TERMINAL_STATUSES set
    (aborted/failed/skipped/succeeded/success/upstream_failed — 6 values) —
    the hardcoded version had only 5, missing ':succeeded' entirely, despite
    this function's own docstring promising "ALL terminal statuses". Currently
    harmless in practice ('succeeded' is a declared-but-never-actually-written
    status at runtime, confirmed against every SFN template — same as
    'pending' elsewhere in this codebase), but any future addition to the
    canonical set would have silently NOT been reflected here without
    deriving both from TASK_TERMINAL_STATUSES directly."""

    def test_condition_expression_values_cover_every_canonical_terminal_status(self):
        from task_actions import build_condition_expression_values
        from constants import TASK_TERMINAL_STATUSES

        values = build_condition_expression_values()
        assert set(values.keys()) == {f':{s}' for s in TASK_TERMINAL_STATUSES}
        assert set(values.values()) == set(TASK_TERMINAL_STATUSES)

    def test_succeeded_is_present_not_just_success(self):
        """The exact drift found: ':succeeded' (distinct from ':success')
        was silently missing from both the expression string and the values
        dict."""
        from task_actions import build_condition_expression_values, TERMINAL_CONDITION_EXPRESSION

        assert ':succeeded' in build_condition_expression_values()
        assert ':succeeded' in TERMINAL_CONDITION_EXPRESSION

    def test_expression_placeholders_match_values_dict_keys_exactly(self):
        """Every :placeholder referenced in the expression string must have a
        corresponding key in the values dict, and vice versa — a mismatch
        either way means DynamoDB rejects the update_item call outright."""
        import re
        from task_actions import build_condition_expression_values, TERMINAL_CONDITION_EXPRESSION

        placeholders_in_expr = set(re.findall(r':\w+', TERMINAL_CONDITION_EXPRESSION))
        placeholders_in_values = set(build_condition_expression_values().keys())
        assert placeholders_in_expr == placeholders_in_values

    def test_base_values_merge_without_dropping_terminal_values(self):
        from task_actions import build_condition_expression_values

        merged = build_condition_expression_values({':status': 'running'})
        assert merged[':status'] == 'running'
        assert ':succeeded' in merged  # terminal values still present
