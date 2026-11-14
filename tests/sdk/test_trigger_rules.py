"""
Trigger rule names in the SDK's validation (ADR #117).

The rules' semantics have one implementation, polyris/coordination/
trigger_rules.py (ADR #130), tested exhaustively in
tests/sdk/test_coordination_trigger_rules.py. These tests pin what the SDK
accepts and rejects at validation time.
"""

# The 5 supported trigger rules (ADR #117 — each produces a distinct, reachable
# behavior under the intervention-first model; see docs/features/DSL.md)
TRIGGER_RULES = [
    'all_success',
    'one_success',
    'all_done',
    'all_skipped',
    'none_skipped',
]


def test_removed_rules_are_rejected_by_validation():
    """ADR #117: a task built with one of the 6 removed rule names must fail
    validate_asl_from_dag with a specific, helpful message — not silently pass
    through and fall back to all_success at runtime (the reconciler's own
    fallback, which exists for a genuinely unrecognized string, not a
    deliberately-removed one the user should be told about)."""
    from polyris import DAG, task
    from polyris.validation import validate_asl_from_dag

    removed_rules_and_expected_hints = {
        'one_done': 'all_done',
        'none_failed': 'all_done',
        'none_failed_min_one_success': 'one_success',
        'all_done_min_one_success': 'one_success',
        'all_failed': 'never satisfiable',
        'one_failed': 'never satisfiable',
    }
    arn = "arn:aws:states:us-east-1:000000000000:stateMachine:x"

    for removed_rule, expected_hint in removed_rules_and_expected_hints.items():
        with DAG(f"removed-rule-{removed_rule}") as dag:
            @task.sfn(arn=arn)
            def upstream():
                pass

            @task.sfn(arn=arn, trigger_rule=removed_rule)
            def downstream():
                pass
            downstream(upstream())

        is_valid, errors, _ = validate_asl_from_dag(dag, verbose=False)
        assert not is_valid, f"{removed_rule} should have failed validation"
        assert any(removed_rule in e and expected_hint in e for e in errors), (
            f"Expected an error mentioning {removed_rule!r} and {expected_hint!r}, got: {errors}"
        )
    print(f"✅ All {len(removed_rules_and_expected_hints)} removed rules correctly rejected")


def test_kept_rules_still_pass_validation():
    """Control: none of the 5 kept rules should be affected by the new
    trigger_rule validation added for ADR #117."""
    from polyris import DAG, task
    from polyris.validation import validate_asl_from_dag

    arn = "arn:aws:states:us-east-1:000000000000:stateMachine:x"
    for rule in TRIGGER_RULES:
        with DAG(f"kept-rule-{rule}") as dag:
            @task.sfn(arn=arn)
            def upstream():
                pass

            @task.sfn(arn=arn, trigger_rule=rule)
            def downstream():
                pass
            downstream(upstream())

        is_valid, errors, _ = validate_asl_from_dag(dag, verbose=False)
        assert is_valid, f"{rule} unexpectedly failed validation: {errors}"
    print(f"✅ All {len(TRIGGER_RULES)} kept rules still pass validation")


def test_genuinely_unknown_rule_gets_a_generic_error_not_a_removal_suggestion():
    """A trigger_rule that is a typo is a different case from a *removed*
    rule (ADR #117) — it should
    get the generic "not a recognized rule, valid rules are: ..." message,
    not a specific "was removed, use X instead" suggestion."""
    from polyris import DAG, task
    from polyris.validation import validate_asl_from_dag

    arn = "arn:aws:states:us-east-1:000000000000:stateMachine:x"
    with DAG("typo-rule") as dag:
        @task.sfn(arn=arn)
        def upstream():
            pass

        @task.sfn(arn=arn, trigger_rule="all_succes")  # typo, never existed
        def downstream():
            pass
        downstream(upstream())

    is_valid, errors, _ = validate_asl_from_dag(dag, verbose=False)
    assert not is_valid
    assert any("not a recognized rule" in e and "Valid rules:" in e for e in errors), errors
    assert not any("was removed" in e for e in errors), errors
    print("✅ Genuinely unknown rule gets the generic error, not a removal suggestion")
