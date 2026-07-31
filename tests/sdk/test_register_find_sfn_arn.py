"""find_sfn_arn must actually use the `namespace` parameter to disambiguate.

Regression test for a real bug found in a code-review pass: `namespace` was
accepted as a parameter (and documented in the function's own docstring, the
inline comment above the matching logic, and the `polyris-register --namespace`
CLI flag's --help text) but never actually referenced in the matching logic
itself. Two different namespaces/teams each registering a pipeline with the
same dag_id (e.g. both team-a and team-b have an "orders" pipeline) would
silently resolve to whichever one the AWS ListStateMachines API happened to
list first — regardless of which namespace was explicitly requested — with no
error or warning. A user running `polyris-register --name orders --namespace
team-b` could silently register team-a's pipeline instead.
"""

import polyris.register as register


def _paginated(mocker, state_machines):
    """Build a mock SFN client whose list_state_machines paginator yields the
    given state machines in a single page."""
    sfn = mocker.MagicMock()
    paginator = mocker.MagicMock()
    sfn.get_paginator.return_value = paginator
    paginator.paginate.return_value = [{"stateMachines": state_machines}]
    return sfn


def test_namespace_disambiguates_colliding_dag_ids(mocker):
    mocker.patch(
        "polyris.register.get_sfn_client",
        return_value=_paginated(mocker, [
            {"name": "team-a-prod-polyris-orders", "stateMachineArn": "arn:a-orders"},
            {"name": "team-b-prod-polyris-orders", "stateMachineArn": "arn:b-orders"},
        ]),
    )

    result_a = register.find_sfn_arn(name="orders", region="us-east-1", namespace="team-a")
    result_b = register.find_sfn_arn(name="orders", region="us-east-1", namespace="team-b")

    assert result_a == "arn:a-orders"
    assert result_b == "arn:b-orders"
    assert result_a != result_b


def test_no_namespace_falls_back_to_first_match(mocker):
    """Control: omitting namespace entirely must preserve the original
    (broader, first-match) search behavior — the fix must not require
    namespace to find anything."""
    mocker.patch(
        "polyris.register.get_sfn_client",
        return_value=_paginated(mocker, [
            {"name": "team-a-prod-polyris-orders", "stateMachineArn": "arn:a-orders"},
            {"name": "team-b-prod-polyris-orders", "stateMachineArn": "arn:b-orders"},
        ]),
    )

    result = register.find_sfn_arn(name="orders", region="us-east-1")
    assert result == "arn:a-orders"  # first match, unfiltered


def test_namespace_that_matches_nothing_returns_none(mocker):
    mocker.patch(
        "polyris.register.get_sfn_client",
        return_value=_paginated(mocker, [
            {"name": "team-a-prod-polyris-orders", "stateMachineArn": "arn:a-orders"},
        ]),
    )

    result = register.find_sfn_arn(name="orders", region="us-east-1", namespace="team-z")
    assert result is None


def test_exact_name_match_still_works_with_namespace(mocker):
    """A state machine literally named "orders" (not the usual
    namespace-stage-polyris-dagid pattern) still matches via the `==` branch,
    as long as it also satisfies the namespace-prefix filter."""
    mocker.patch(
        "polyris.register.get_sfn_client",
        return_value=_paginated(mocker, [
            {"name": "team-a-orders", "stateMachineArn": "arn:exact"},
        ]),
    )

    result = register.find_sfn_arn(name="team-a-orders", region="us-east-1", namespace="team-a")
    assert result == "arn:exact"
