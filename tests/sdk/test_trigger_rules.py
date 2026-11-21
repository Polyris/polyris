"""
Trigger rule names in the SDK's validation (ADR #138).

The rules' semantics have one implementation, polyris/coordination/
trigger_rules.py, tested cell by cell in
tests/sdk/test_coordination_trigger_rules.py. These tests pin what the SDK
accepts and rejects at validation time.
"""

import pytest

from polyris.coordination import trigger_rules

ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:x"


def validate_rule(rule):
    from polyris import DAG, task
    from polyris.validation import validate_asl_from_dag

    with DAG(f"rule-{rule}".replace("_", "-")) as dag:
        @task.sfn(arn=ARN)
        def upstream():
            pass

        @task.sfn(arn=ARN, trigger_rule=rule)
        def downstream():
            pass
        downstream(upstream())
    is_valid, errors, _ = validate_asl_from_dag(dag, verbose=False)
    return is_valid, errors


@pytest.mark.parametrize("rule", sorted(trigger_rules.KNOWN_RULES))
def test_each_of_the_nine_rules_passes_validation(rule):
    is_valid, errors = validate_rule(rule)
    assert is_valid, f"{rule} unexpectedly failed validation: {errors}"


@pytest.mark.parametrize("rule,hint", [
    ("one_done", "'all_done' to wait for every upstream"),
    ("all_done_min_one_success", "'one_success'"),
    ("always", "remove the dependency"),
    ("all_done_setup_success", "'all_done' for a cleanup task"),
])
def test_a_name_adr_138_turned_down_is_rejected_with_its_hint(rule, hint):
    """Not passed through to fail the task at run time (ADR #138 §2), and not the generic message."""
    is_valid, errors = validate_rule(rule)
    assert not is_valid
    assert any(rule in e and "not supported (ADR #138)" in e and hint in e for e in errors), errors


def test_a_typo_gets_the_list_of_rules_not_a_hint():
    is_valid, errors = validate_rule("all_succes")
    assert not is_valid
    assert any("not a recognized rule" in e and "Valid rules:" in e for e in errors), errors
    assert not any("not supported" in e for e in errors), errors
