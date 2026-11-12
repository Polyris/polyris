"""ReconcileFunction and ReconcileRole in sam/template.yaml match the code (Principle #13).

The IAM test records every DynamoDB call the reconciler makes while it walks
each of its paths, then compares that set with the role: a call the role
does not allow would be an AccessDenied in production, and a grant no call
uses is privilege the function does not need.
"""

import json
import re
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Set, Tuple

import pytest
from coordination_testkit import (
    LAMBDA_ROOT,
    NOW,
    RUN,
    SHORT,
    TABLES,
    FakeSfn,
    execution_name,
    legacy_fact,
    task_row,
    template,
)

import env
import reconcile

LOGICAL = {name: logical for logical, name in TABLES.values()}
Call = Tuple[str, str, Optional[str]]   # (IAM action, table logical id, index or None)


@pytest.fixture(scope="module")
def resources() -> Dict[str, Any]:
    return template()["Resources"]


@contextmanager
def recording(ddb) -> Iterator[Set[Call]]:
    calls: Set[Call] = set()

    def before(params: Dict[str, Any], model: Any, **_: Any) -> None:
        tables = params["RequestItems"] if model.name == "BatchGetItem" else [params["TableName"]]
        for table in tables:
            calls.add((f"dynamodb:{model.name}", LOGICAL[table], params.get("IndexName")))

    events = ddb.meta.client.meta.events
    events.register("before-parameter-build.dynamodb", before, unique_id="coordination-iam")
    try:
        yield calls
    finally:
        events.unregister("before-parameter-build.dynamodb", unique_id="coordination-iam")


def _resource(value: Any) -> Tuple[str, Optional[str]]:
    """``{"!GetAtt": "T.Arn"}`` → (T, None); ``{"!Sub": "${T.Arn}/index/I"}`` → (T, I)."""
    if "!GetAtt" in value:
        return value["!GetAtt"].removesuffix(".Arn"), None
    match = re.fullmatch(r"\$\{(\w+)\.Arn\}/index/([\w-]+)", value["!Sub"])
    assert match, f"unexpected DynamoDB resource {value}"
    return match.group(1), match.group(2)


def _statements(resources: Dict[str, Any]):
    return resources["ReconcileRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]


def _walk_every_path(reconciler, tokens, subscriptions, asset_events, registry, sfn, ddb) -> Set[Call]:
    load, report = execution_name("load"), execution_name("report")
    tokens.put_item(Item=task_row("extract", status="success"))
    tokens.put_item(Item=task_row("load", deps=["extract"], wait_for=[{"asset_name": "ns/orders", "freshness_hours": None}]))
    subscriptions.put_item(Item={"dependency_key": f"extract-{SHORT}", "subscriber": load})
    registry.put_item(Item={"pipeline_name": "producer", "dag": json.dumps({"nodes": []})})
    asset_events.put_item(Item=legacy_fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:55:00.000Z"))
    with recording(ddb) as calls:
        assert reconciler.reconcile(load, source="registration")["action"] == "signalled"

    tokens.put_item(Item={"execution_name": f"_pause_{RUN}", "paused": True})
    tokens.put_item(Item=task_row("report"))
    with recording(ddb) as more:
        assert reconciler.reconcile(report, source="registration")["action"] == "parked"
    calls |= more

    zombie = task_row("zombie")
    tokens.put_item(Item=zombie)
    tokens.delete_item(Key={"execution_name": f"_pause_{RUN}"})
    sfn.dead_tokens.add(zombie["wait_token"])
    with recording(ddb) as more:
        assert reconciler.reconcile(zombie["execution_name"], source="sweeper")["action"] == "aborted"
    calls |= more

    tokens.put_item(Item=task_row("done", status="success", attention="waiting#1", attention_at="t"))
    tokens.put_item(Item=task_row("late", attention="waiting#1", attention_at="t"))
    with recording(ddb) as more:
        assert reconciler.reconcile(execution_name("done"), source="sweeper")["action"] == "attention_cleared"
        reconcile.Reconciler(ddb, sfn, clock=lambda: NOW, budget_seconds=0).reconcile_run(RUN, source="resume")
    return calls | more


def test_reconcile_role_allows_exactly_the_calls_the_code_makes(
        resources, reconciler, tokens, subscriptions, asset_events, registry, sfn, ddb):
    used = _walk_every_path(reconciler, tokens, subscriptions, asset_events, registry, sfn, ddb)
    granted: Set[Call] = set()
    for statement in _statements(resources):
        for action in (a for a in statement["Action"] if a.startswith("dynamodb:")):
            granted |= {(action, *_resource(r)) for r in statement["Resource"]}
    assert used - granted == set(), "calls the role does not allow (AccessDenied in production)"
    assert granted - used == set(), "grants no call uses (drop them, or walk the path that needs them)"


def test_reconcile_role_step_functions_actions(resources):
    actions = {a for s in _statements(resources) for a in s["Action"] if a.startswith("states:")}
    used = {"states:" + "".join(p.title() for p in m.split("_"))
            for m in vars(FakeSfn) if not m.startswith("_")}
    assert actions == used == {"states:SendTaskSuccess", "states:SendTaskFailure", "states:DescribeExecution"}


def test_describe_execution_is_scoped_to_wrapper_executions(resources):
    wrapper = next(r for r in resources.values()
                   if r["Type"] == "AWS::Serverless::StateMachine"
                   and r["Properties"]["Name"]["!Sub"].endswith("-polyris-dependency-wrapper"))
    statement = next(s for s in _statements(resources) if s["Action"] == ["states:DescribeExecution"])
    [arn] = statement["Resource"]
    assert arn["!Sub"].endswith(f":execution:{wrapper['Properties']['Name']['!Sub']}:*")
    assert "${AWS::Region}" in arn["!Sub"]      # not the AwsRegion parameter, which can drift


def test_reconcile_function_wiring(resources):
    props = resources["ReconcileFunction"]["Properties"]
    module, function = props["Handler"].split(".")
    assert (LAMBDA_ROOT.parent.parent / props["CodeUri"]).resolve() == LAMBDA_ROOT
    assert callable(getattr(reconcile, function)) and module == "reconcile"
    assert props["Role"] == {"!GetAtt": "ReconcileRole.Arn"}
    variables = props["Environment"]["Variables"]
    assert variables == {var: {"!Ref": logical} for var, (logical, _) in TABLES.items()}
    assert {var: getattr(env, var) for var in variables} == {var: name for var, (_, name) in TABLES.items()}
    # Resume's budget fits inside the timeout with room for the last row.
    assert reconcile.RESUME_BUDGET_SECONDS + 5 <= props["Timeout"]
