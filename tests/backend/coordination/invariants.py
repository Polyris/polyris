"""The template expressions known to fail, which the World's check of every expression skips (D107).

The World fails a scenario on any template expression that failed, caught or not
(``World._expression_failures``). The interpreter fails a state the way AWS does, an
expression that evaluates to undefined included, and such a failure is always a
template bug. A catch can hide it: `Failure_Complete` read fields its input no
longer had, and every scenario passed while the live failure handler failed
(1.2.0, CR-7). ADR #130's invariants, which this module held too, retired with
its engine (D115).
"""

from typing import Dict, Tuple

#: Expressions known to fail in templates that 1.3 deletes, keyed by the machine's name after "polyris-"
#: and the state, with the template's name in ADR #137's *What is removed*. The expression check skips
#: these and nothing else; TR-8 deletes them with their templates, and test_meta.py keeps the list to
#: templates the ADR removes.
KNOWN_FAILING_EXPRESSIONS: Dict[Tuple[str, str], str] = {
    # Reads pipeline_name, which Run_Task's output does not carry, so the PagerDuty resolve after a
    # success never ran in 1.2: its Catch went on. run_task's call after a success replaces it (ADR #137 §1).
    ("dependency-wrapper", "Resolve_PagerDuty"): "dependency_wrapper",
    ("dependency-wrapper-v1-1-4", "Resolve_PagerDuty"): "dependency_wrapper",
    # A row with no run_task ARN fails the state on purpose, and its Catch goes on to the hand-over.
    ("restart-task-helper", "Stop_Old_Inner_Wrapper"): "restart_task",
}
