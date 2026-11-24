"""Trigger-rule reference — every fan-in condition in one DAG.

A downstream task's `trigger_rule` decides *when* it runs from its direct
upstream tasks' outcomes. The default is `all_success`. Two upstream tasks
(`extract_a`, `extract_b`) feed one marker task per rule, so you can see each
condition side by side.

A rule reads final outcomes. A task that fails first waits for a human
decision, and only a Fail, the decision timeout or a Stop makes the failure
final; until then the task counts as pending for every rule. A rule runs its
task as soon as its condition holds, and skips it (or ends it
`upstream_failed`) only once every upstream is final.

  all_success                  every upstream succeeded, or an operator or the run skipped it (DEFAULT)
  one_success                  one upstream succeeded; runs at the first success
  all_done                     every upstream is final, whatever the outcome (cleanup)
  all_skipped                  every upstream was skipped
  none_skipped                 every upstream is final and none was skipped
  one_failed                   one upstream failed finally; runs at the first final failure
  all_failed                   every upstream failed finally
  none_failed                  every upstream is final and none failed; skips pass
  none_failed_min_one_success  as none_failed, and at least one upstream succeeded

Any other name fails validation; four that other orchestrators have get a hint
for the rule to use instead. See docs/features/DSL.md#trigger-rules.

Run it locally (no AWS):  polyris-output --graph
"""
from polyris import DAG, task

# NOTE: placeholder ARN — replace with your own state machine before deploying.
ARN = "arn:aws:states:us-east-1:000000000000:stateMachine:polyris-test-sfn"

with DAG(
    dag_id="trigger-rules-reference",
    group="features",
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
        """Runs only if both extracts succeeded (this is the default rule)."""
        pass

    @task.sfn(arn=ARN, trigger_rule="one_success")
    def on_one_success():
        """Fires as soon as one upstream succeeds — first-wins pattern."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_done")
    def on_all_done():
        """Runs once both are final, whatever the outcome — the cleanup rule.
        A failure counts once it is final: after the decision on it."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_skipped")
    def on_all_skipped():
        """Runs only if both upstreams were skipped."""
        pass

    @task.sfn(arn=ARN, trigger_rule="none_skipped")
    def on_none_skipped():
        """Runs once both are final and neither was skipped; failures pass."""
        pass

    @task.sfn(arn=ARN, trigger_rule="one_failed")
    def on_one_failed():
        """Compensation: runs at the first final failure, for example to delete
        a half-written partition. The console bell, not this task, is the alert."""
        pass

    @task.sfn(arn=ARN, trigger_rule="all_failed")
    def on_all_failed():
        """Fallback: runs only if both upstreams failed finally."""
        pass

    @task.sfn(arn=ARN, trigger_rule="none_failed")
    def on_none_failed():
        """Runs once both are final and neither failed; skips pass, even two."""
        pass

    @task.sfn(arn=ARN, trigger_rule="none_failed_min_one_success")
    def on_none_failed_min_one_success():
        """As none_failed, and at least one upstream succeeded."""
        pass

    # Every marker depends on the same two extracts; only the trigger_rule differs.
    upstreams = [extract_a(), extract_b()]
    on_all_success(upstreams)
    on_one_success(upstreams)
    on_all_done(upstreams)
    on_all_skipped(upstreams)
    on_none_skipped(upstreams)
    on_one_failed(upstreams)
    on_all_failed(upstreams)
    on_none_failed(upstreams)
    on_none_failed_min_one_success(upstreams)
