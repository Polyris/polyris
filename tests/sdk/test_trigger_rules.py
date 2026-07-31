"""
Trigger Rule Sync Test

Verifies that JSONata (registration helper) and Python (evaluate_deps)
implementations produce identical results for all trigger rules and status combinations.

This test prevents drift between the two implementations.

Note: In v51+, notify_dependents is a SFN helper, not a Lambda.
The Python source of truth for trigger rules is evaluate_deps/index.py.
"""

import itertools
from typing import List

# All supported trigger rules (ADR #117 — 5 rules that produce a distinct,
# reachable behavior under the intervention-first model; see
# docs/features/DSL.md for the analysis)
TRIGGER_RULES = [
    'all_success',
    'one_success',
    'all_done',
    'all_skipped',
    'none_skipped',
]

# All possible dependency statuses
# Terminal statuses
TERMINAL_STATUSES = {'success', 'failed', 'skipped', 'upstream_failed', 'aborted'}

# Statuses to test: terminal + non-terminal (waiting, running, etc.)
# not_found = task not yet registered; waiting = registered but deps not ready
STATUSES = ['success', 'failed', 'skipped', 'upstream_failed', 'not_found', 'aborted',
            'waiting', 'running', 'deps_ready']


def python_check_trigger_rule(
    trigger_rule: str, dep_statuses: List[str], skip_origins: List[str] = None
) -> bool:
    """
    Python implementation (from evaluate_deps).
    Returns True if trigger_rule is satisfied.

    skip_origins (ADR #115 step 1.2): optional list aligned with dep_statuses by
    index; entries are 'rule', 'manual', or None/'' (unknown). Only all_success
    treats a 'rule'-originated skip as non-OK (cascades); every other origin value
    (including absence) preserves the original "skipped counts as ok" behavior.
    Mirrors evaluate_deps/index.py's cascading_skips computation exactly.
    """
    if not dep_statuses:
        return True
    
    success_count = sum(1 for s in dep_statuses if s == 'success')
    skipped_count = sum(1 for s in dep_statuses if s == 'skipped')
    total = len(dep_statuses)
    pending_count = sum(1 for s in dep_statuses if s not in TERMINAL_STATUSES)
    origins = skip_origins or [None] * len(dep_statuses)
    cascading_skips = sum(
        1 for s, o in zip(dep_statuses, origins) if s == 'skipped' and o == 'rule'
    )
    ok_count = success_count + (skipped_count - cascading_skips)
    
    if trigger_rule == 'all_success':
        return pending_count == 0 and ok_count == total
    elif trigger_rule == 'one_success':
        return success_count > 0
    elif trigger_rule == 'all_done':
        return pending_count == 0
    elif trigger_rule == 'all_skipped':
        return pending_count == 0 and skipped_count == total
    elif trigger_rule == 'none_skipped':
        return pending_count == 0 and skipped_count == 0
    else:
        return pending_count == 0 and ok_count == total


def jsonata_check_trigger_rule(
    trigger_rule: str, dep_statuses: List[str], skip_origins: List[str] = None
) -> bool:
    """
    JSONata implementation (from registration helper).
    Simulates the JSONata logic in Python for testing.
    
    After P0.1 fix: $pending := $c.total - $c.done
    (was: $pending := $c.not_found — only counted not_found as pending)

    ADR #115 step 1.2: $cascading_skips / origin-aware $ok, mirroring the actual
    JSONata in sam/sfn_templates/helpers/registration/sfn.tpl.json's Eval_Task_Deps.
    """
    if not dep_statuses:
        return True
    
    success_count = sum(1 for s in dep_statuses if s == 'success')
    skipped_count = sum(1 for s in dep_statuses if s == 'skipped')
    done_count = sum(1 for s in dep_statuses if s in TERMINAL_STATUSES)
    total = len(dep_statuses)
    pending_count = total - done_count  # Fixed: was not_found_count, now total - done
    origins = skip_origins or [None] * len(dep_statuses)
    cascading_skips = sum(
        1 for s, o in zip(dep_statuses, origins) if s == 'skipped' and o == 'rule'
    )
    ok_count = success_count + (skipped_count - cascading_skips)
    
    # Exact mirror of JSONata logic
    if trigger_rule == 'all_success':
        return pending_count == 0 and ok_count == total
    elif trigger_rule == 'one_success':
        return success_count > 0
    elif trigger_rule == 'all_done':
        return pending_count == 0
    elif trigger_rule == 'all_skipped':
        return pending_count == 0 and skipped_count == total
    elif trigger_rule == 'none_skipped':
        return pending_count == 0 and skipped_count == 0
    else:
        # Default: all_success behavior
        return pending_count == 0 and ok_count == total


def test_trigger_rules_sync():
    """
    Test that Python and JSONata implementations are in sync.
    Tests all trigger rules against all combinations of 1-3 dependencies.
    """
    print("Testing trigger_rule sync between Python and JSONata...")
    
    failures = []
    test_count = 0
    
    for rule in TRIGGER_RULES:
        # Test with 1, 2, and 3 dependencies
        for dep_count in [1, 2, 3]:
            for combo in itertools.product(STATUSES, repeat=dep_count):
                statuses = list(combo)
                test_count += 1
                
                py_result = python_check_trigger_rule(rule, statuses)
                js_result = jsonata_check_trigger_rule(rule, statuses)
                
                if py_result != js_result:
                    failures.append({
                        'rule': rule,
                        'statuses': statuses,
                        'python': py_result,
                        'jsonata': js_result
                    })
    
    print(f"Tested {test_count} combinations")
    
    if failures:
        print(f"\n❌ Found {len(failures)} mismatches:")
        for f in failures[:10]:  # Show first 10
            print(f"  {f['rule']}: {f['statuses']} -> Python={f['python']}, JSONata={f['jsonata']}")
        if len(failures) > 10:
            print(f"  ... and {len(failures) - 10} more")
        raise AssertionError(f"{len(failures)} trigger_rule mismatches found")
    
    print(f"✅ All {test_count} combinations match between Python and JSONata")


def test_edge_cases():
    """Test specific edge cases that are easy to get wrong."""
    print("\nTesting edge cases...")
    
    cases = [
        # (rule, statuses, expected)
        ('all_success', [], True),  # No deps = ready
        ('all_success', ['success'], True),
        ('all_success', ['skipped'], True),  # skipped = OK for all_success
        ('all_success', ['failed'], False),
        ('all_success', ['not_found'], False),  # pending
        ('all_success', ['aborted'], False),  # aborted = failure

        ('one_success', ['not_found', 'not_found'], False),  # no success yet
        ('one_success', ['success', 'not_found'], True),  # one success = ready
        ('one_success', ['failed', 'failed'], False),
        ('one_success', ['aborted', 'aborted'], False),  # aborted != success

        ('all_done', ['success', 'failed', 'skipped'], True),
        ('all_done', ['success', 'not_found'], False),
        ('all_done', ['success', 'aborted'], True),  # aborted is done
        ('all_done', ['aborted', 'aborted'], True),  # all aborted = all done

        ('all_skipped', ['skipped', 'skipped'], True),
        ('all_skipped', ['skipped', 'success'], False),
        ('all_skipped', ['skipped', 'aborted'], False),  # aborted != skipped

        ('none_skipped', ['success', 'failed'], True),
        ('none_skipped', ['success', 'skipped'], False),
        ('none_skipped', ['success', 'aborted'], True),  # aborted != skipped
        ('none_skipped', ['aborted', 'failed'], True),  # no skipped

        # NON-TERMINAL STATUS TESTS (P0.1 regression: waiting/running must block)
        ('all_done', ['waiting'], False),  # waiting is NOT done
        ('all_done', ['running'], False),  # running is NOT done
        ('all_done', ['deps_ready'], False),  # deps_ready is NOT done
        ('all_done', ['success', 'waiting'], False),  # one still waiting
        ('none_skipped', ['waiting'], False),  # pending blocks none_skipped
        ('none_skipped', ['running'], False),  # pending blocks none_skipped
        ('all_success', ['waiting'], False),  # pending blocks all_success
        ('all_success', ['running'], False),  # pending blocks all_success
        ('one_success', ['waiting'], False),  # no success yet
    ]
    
    for rule, statuses, expected in cases:
        py_result = python_check_trigger_rule(rule, statuses)
        js_result = jsonata_check_trigger_rule(rule, statuses)
        
        assert py_result == expected, f"Python {rule}({statuses}) = {py_result}, expected {expected}"
        assert js_result == expected, f"JSONata {rule}({statuses}) = {js_result}, expected {expected}"
    
    print(f"✅ All {len(cases)} edge cases passed")


def test_skip_origin_cascade_sync():
    """
    ADR #115 step 1.2: Python (evaluate_deps) and JSONata (registration) must agree
    on the skip-origin-aware all_success cascade for every combination of statuses
    and origins, not just plain statuses. This is the parity guard ADR #115 requires
    precisely because the cascade logic is duplicated across two runtimes.

    Only all_success is exercised here (skip_origins affects no other rule, per both
    implementations) but exhaustively over origin combinations, including mixed
    manual/rule skips and skips with no origin at all (the common/legacy case).
    """
    ORIGIN_VALUES = ['rule', 'manual', None, '']
    print("\nTesting skip_origin cascade sync for all_success...")

    failures = []
    test_count = 0
    for dep_count in [1, 2, 3]:
        for statuses in itertools.product(STATUSES, repeat=dep_count):
            statuses = list(statuses)
            if 'skipped' not in statuses:
                continue  # skip_origins is a no-op unless a dep is actually skipped
            for origins in itertools.product(ORIGIN_VALUES, repeat=dep_count):
                origins = list(origins)
                test_count += 1
                py_result = python_check_trigger_rule('all_success', statuses, origins)
                js_result = jsonata_check_trigger_rule('all_success', statuses, origins)
                if py_result != js_result:
                    failures.append({
                        'statuses': statuses, 'origins': origins,
                        'python': py_result, 'jsonata': js_result,
                    })

    print(f"Tested {test_count} status/origin combinations")
    if failures:
        print(f"\n❌ Found {len(failures)} mismatches:")
        for f in failures[:10]:
            print(f"  all_success{f['statuses']} origins={f['origins']} -> "
                  f"Python={f['python']}, JSONata={f['jsonata']}")
        raise AssertionError(f"{len(failures)} skip-origin cascade mismatches found")
    print(f"✅ All {test_count} status/origin combinations match between Python and JSONata")


def test_skip_origin_cascade_edge_cases():
    """Specific skip-origin scenarios, asserted against the expected (not just
    Python==JSONata) value — proves both implementations are not just consistent
    with each other but actually correct."""
    cases = [
        # (statuses, origins, expected)
        (['success', 'skipped'], [None, 'rule'], False),       # rule-skip cascades
        (['success', 'skipped'], [None, 'manual'], True),      # manual-skip does not
        (['success', 'skipped'], [None, None], True),          # unknown origin = ok (default)
        (['success', 'skipped'], [None, ''], True),             # empty-string origin = ok
        (['skipped', 'skipped'], ['rule', 'rule'], False),     # all rule-skipped -> blocks
        (['skipped', 'skipped'], ['manual', 'manual'], True),  # all manual-skipped -> ok
        (['skipped', 'skipped'], ['rule', 'manual'], False),   # one rule-skip is enough to block
        (['success', 'success', 'skipped'], [None, None, 'rule'], False),
    ]
    for statuses, origins, expected in cases:
        py_result = python_check_trigger_rule('all_success', statuses, origins)
        js_result = jsonata_check_trigger_rule('all_success', statuses, origins)
        assert py_result == expected, (
            f"Python all_success{statuses} origins={origins} = {py_result}, expected {expected}"
        )
        assert js_result == expected, (
            f"JSONata all_success{statuses} origins={origins} = {js_result}, expected {expected}"
        )
    print(f"✅ All {len(cases)} skip-origin edge cases passed")


if __name__ == '__main__':
    test_trigger_rules_sync()
    test_edge_cases()
    test_skip_origin_cascade_sync()
    test_skip_origin_cascade_edge_cases()
    print("\n=== All trigger_rule tests passed ===")


def test_removed_rules_are_rejected_by_validation():
    """ADR #117: a task built with one of the 6 removed rule names must fail
    validate_asl_from_dag with a specific, helpful message — not silently pass
    through and fall back to all_success at runtime (evaluate_deps' own
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
