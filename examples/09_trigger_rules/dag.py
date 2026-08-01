"""Trigger-rule reference — every fan-in condition in one DAG.

A downstream task's `trigger_rule` decides *when* it runs relative to its
upstream tasks' outcomes. The default is `all_success`. Two upstream tasks
(`extract_a`, `extract_b`) feed one marker task per rule, so you can see each
condition side by side.

polyris supports 5 trigger rules (ADR #117). The other 6 possible names are
rejected at validation time with a specific suggestion: under polyris's
intervention-first model, a *confirmed* failure
(resolving a paused task with Fail) cancels the whole pipeline's Parallel
before any downstream trigger_rule ever evaluates. Given that, `one_done`
and `none_failed` always behave identically to `all_done`;
`none_failed_min_one_success` and `all_done_min_one_success` always behave
identically to `one_success`; and `all_failed`/`one_failed` can never be
satisfied at all — their only intended use case (reacting to a confirmed
failure) is exactly the state Parallel-abort prevents them from reaching.
See docs/features/DSL.md#trigger-rules for the full analysis.

  all_success   all upstreams succeeded (DEFAULT)
  one_success   at least one upstream succeeded (doesn't wait for the rest)
  all_done      all upstreams finished, any status (cleanup)
  all_skipped   all upstreams were skipped
  none_skipped  no upstream was skipped

Run it locally (no AWS):  polyris-output --graph
"""
from polyris import DAG, task

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="trigger-rules-reference",
    schedule="@daily",
    description="One downstream marker per trigger rule, fed by two extracts.",
) as dag:

    @task.sfn(arn=ARN)
    def extract_a():
        """First upstream source."""
        pass

    @task.sfn(arn=ARN)
    def extract_b():
        """Second upstream source."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_success")
    def on_all_success():
        """Runs only if BOTH extracts succeeded (this is the default rule)."""
        pass

    @task.sfn(arn=ARN, trigger_rule="one_success")
    def on_one_success():
        """Fires as soon as any upstream succeeds — first-wins pattern."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_done")
    def on_all_done():
        """Runs once both finished, success or skip — the standard cleanup
        rule. Reacting specifically to a confirmed failure isn't possible
        today: that failure cancels this marker's branch before it can
        evaluate (see the module docstring above)."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_skipped")
    def on_all_skipped():
        """Runs only if both upstreams were skipped."""
        pass

    @task.sfn(arn=ARN, trigger_rule="none_skipped")
    def on_none_skipped():
        """Runs if neither upstream was skipped (failures/successes are both
        fine, as long as nothing was skipped)."""
        pass

    # Every marker depends on the same two extracts; only the trigger_rule differs.
    upstreams = [extract_a(), extract_b()]
    on_all_success(upstreams)
    on_one_success(upstreams)
    on_all_done(upstreams)
    on_all_skipped(upstreams)
    on_none_skipped(upstreams)
