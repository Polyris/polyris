"""notify_dependents: a hint path that retries what is transient and loses the rest to the sweeper.

ADR #130 §6.2, Principles #41 and #53. tests/backend/coordination runs the whole template.
"""
from sfn_jsonata import LAMBDA_TRANSIENT, load_template

from polyris.coordination import wake


def _states():
    template = load_template("helpers/notify_dependents/sfn.tpl.json")
    return template["States"], template["States"]["Reconcile_Subscribers"]["ItemProcessor"]["States"]


def test_the_transient_set_names_the_coordination_error():
    assert wake.TRANSIENT_ERROR in LAMBDA_TRANSIENT


def test_each_subscriber_reconcile_retries_every_transient_failure():
    _, inner = _states()
    assert set(inner["Reconcile"]["Retry"][0]["ErrorEquals"]) == LAMBDA_TRANSIENT


def test_a_failed_subscriber_query_is_retried_before_it_is_lost():
    """query_subscriptions raises on any failure instead of answering with no subscribers."""
    states, _ = _states()
    [retry] = states["Query_Subscriptions"]["Retry"]
    assert {"States.TaskFailed", "Lambda.Unknown", "Sandbox.Timedout"} <= set(retry["ErrorEquals"])
    assert retry["MaxAttempts"] >= 2
