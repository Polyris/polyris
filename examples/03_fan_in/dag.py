"""Fan-out / fan-in with a trigger rule.

Three extracts run in parallel; a final `merge` task depends on all three.
`trigger_rule="all_done"` means merge runs once every upstream task has
reached a terminal state — success or skip. (A genuine failure pauses for a
human decision first, ADR #114, so `all_done` reacting to that specifically
isn't part of this yet — see docs/features/DSL.md#trigger-rules.)

polyris supports 5 trigger rules in total (ADR #117): `all_success` (the
default), `one_success`, `all_done`, `all_skipped`, `none_skipped` — see
example 09 (`09_trigger_rules`) for all five side by side, and
docs/features/DSL.md#trigger-rules for the full analysis.

Run it locally (no AWS):  polyris-output --graph
"""
from polyris import DAG, task

# NOTE: ARNs below are hardcoded to the testing-infra CloudFormation stack.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="multi-source-merge",
    schedule="@daily",
    description="Three parallel sources merged by an all_done marker task.",
) as dag:

    @task.sfn(arn=ARN)
    def extract_orders():
        """Extract orders."""
        pass

    @task.sfn(arn=ARN)
    def extract_returns():
        """Extract returns."""
        pass

    @task.sfn(arn=ARN)
    def extract_inventory():
        """Extract inventory."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_done")
    def merge():
        """Combine all three sources once they've all reached a terminal
        state (success or skip).

        A fan-in task reads *every* upstream's output from ``upstream`` (keyed by
        task name), so must tolerate a missing/skipped one — a genuinely failed
        upstream pauses for a decision before `merge` would even be evaluated
        (ADR #114)::

            orders    = $states.input.upstream.extract_orders.output
            returns   = $states.input.upstream.extract_returns.output
            inventory = $states.input.upstream.extract_inventory.output
        """
        pass

    # merge depends on all three parallel extracts (pass them as a list). Each of
    # their outputs arrives under event["upstream"] / $states.input.upstream.
    merge([extract_orders(), extract_returns(), extract_inventory()])
