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
    FUNCTION_TABLES,
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


def _statements(resources: Dict[str, Any], role: str = "ReconcileRole", *, managed: bool = True):
    """The role's statements: its inline policies, and the managed policies it attaches."""
    props = resources[role]["Properties"]
    statements = [s for policy in props.get("Policies", []) for s in policy["PolicyDocument"]["Statement"]]
    for arn in props.get("ManagedPolicyArns", []) if managed else []:
        if isinstance(arn, dict):          # a policy of this template; AWS-managed ones are strings
            statements += resources[arn["!Ref"]]["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _dynamodb_grants(statements) -> Set[Call]:
    return {(action, *_resource(r)) for s in statements for action in s["Action"]
            if action.startswith("dynamodb:") and action not in STREAM_ACTIONS for r in s["Resource"]}


STREAM_ACTIONS = {"dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator",
                  "dynamodb:ListStreams"}


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
    granted = _dynamodb_grants(_statements(resources))
    assert used - granted == set(), "calls the role does not allow (AccessDenied in production)"
    assert granted - used == set(), "grants no call uses (drop them, or walk the path that needs them)"
    assert "Policies" not in resources["ReconcileRole"]["Properties"], "the reconciler's grants live in its policy"


def test_asset_fanout_role_allows_every_call_and_its_own_grants_are_all_used(
        resources, reconciler, tokens, subscriptions, asset_events, sfn, ddb, mocker):
    import asset_fanout
    from coordination_testkit import client_error, fact

    fanout = asset_fanout.AssetFanout(reconciler, subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1])
    tokens.put_item(Item=task_row("load", wait_for=[{"asset_name": "ns/orders", "freshness_hours": None}]))
    subscriptions.put_item(Item={"dependency_key": "asset:ns/orders", "subscriber": execution_name("load")})
    item = fact("ns/orders", "2026-11-12")
    record = {"eventName": "INSERT",
              "dynamodb": {"SequenceNumber": "1", "NewImage": {k: _serialize(v) for k, v in item.items()}}}
    asset_events.put_item(Item=item)
    with recording(ddb) as used:
        assert fanout.handle([record], lambda: 10 ** 6) == {"batchItemFailures": []}
        mocker.patch.object(fanout.subscriptions, "query", side_effect=client_error("AccessDeniedException", "Query"))
        fanout.handle([record], lambda: 10 ** 6)           # a non-transient failure writes a warning
    assert sfn.succeeded, "the walk must wake the task"
    assert used - _dynamodb_grants(_statements(resources, "AssetFanoutRole")) == set()
    own = _dynamodb_grants(_statements(resources, "AssetFanoutRole", managed=False))
    assert own - used == set(), "AssetFanout's own grants that no call uses"


def test_sweeper_role_allows_every_call_and_its_own_grants_are_all_used(
        resources, tokens, asset_events, consumers, registry, sfn, ddb):
    from datetime import timedelta

    import sweeper
    from coordination_core import attention as att
    from coordination_core.times import format_utc
    from coordination_testkit import fact

    clock = NOW + timedelta(hours=74)
    reconciler = reconcile.Reconciler(ddb, sfn, clock=lambda: clock)
    wait_for = [{"asset_name": "ns/orders", "freshness_hours": None}]
    for task in ("load", "zombie"):
        name = execution_name(task)
        tokens.put_item(Item=task_row(task, wait_for=wait_for, attention=att.attention(att.WAITING, name),
                                      attention_at=format_utc(clock - timedelta(minutes=5))))
    sfn.executions[f"arn:aws:states:us-east-1:123456789012:execution:wrapper:{execution_name('load')}"] = "RUNNING"
    with recording(ddb) as used:
        sweeper.Sweeper(reconciler, owner="me").run()            # the lease, a re-check, a zombie
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time="2026-11-12T05:10:00.000Z"))
    tokens.update_item(Key={"execution_name": execution_name("load")}, UpdateExpression="SET #due = :d",
                       ExpressionAttributeNames={"#due": att.DUE},
                       ExpressionAttributeValues={":d": format_utc(clock - timedelta(minutes=5))})
    consumers.put_item(Item={"asset_name": "ns/orders", "pipeline_name": "pushed", "sfn_arn": "arn:pushed"})
    registry.put_item(Item={"pipeline_name": "pushed",
                            "asset_schedule": json.dumps({"operator": "AND", "assets": ["ns/orders"]})})
    from coordination_core.records import push_retry_item
    retry = push_retry_item(run_name="pushed-retry", consumer="pushed", sfn_arn="arn:pushed", run_input="{}",
                            error="ThrottlingException", now=clock - timedelta(hours=1))
    retry[att.DUE] = format_utc(clock - timedelta(minutes=1))
    tokens.put_item(Item=retry)
    tokens.delete_item(Key={"execution_name": "_engine_health"})       # make the catch-up due again
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time=format_utc(clock - timedelta(hours=1))))
    with recording(ddb) as more:
        # A woken task with a lost hint, a push retry, and the catch-up's push.
        sweeper.Sweeper(reconciler, owner="me").run()
    used |= more
    assert sfn.succeeded, "the walk must wake the task"
    assert {s["name"] for s in sfn.started} >= {"pushed-retry"} and len(sfn.started) == 2
    assert used - _dynamodb_grants(_statements(resources, "SweeperRole")) == set()
    own = _dynamodb_grants(_statements(resources, "SweeperRole", managed=False))
    assert own - used == set(), "the sweeper's own grants that no call uses"


def test_the_sweeper_runs_every_5_minutes_without_retries(resources):
    schedule = resources["SweeperSchedule"]["Properties"]
    assert schedule["ScheduleExpression"] == "rate(5 minutes)"
    assert schedule["Target"]["Arn"] == {"!GetAtt": "SweeperFunction.Arn"}
    assert schedule["Target"]["RetryPolicy"] == {"MaximumRetryAttempts": 0}
    [statement] = resources["SweeperScheduleRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert (statement["Action"], statement["Resource"]) == (["lambda:InvokeFunction"], [{"!GetAtt": "SweeperFunction.Arn"}])


def _serialize(value):
    from boto3.dynamodb.types import TypeSerializer

    return TypeSerializer().serialize(value)


def test_asset_fanout_reads_only_the_asset_events_stream(resources):
    [stream] = [s for s in _statements(resources, "AssetFanoutRole", managed=False)
                if set(s["Action"]) == STREAM_ACTIONS]
    assert stream["Resource"] == [{"!GetAtt": "AssetEventsTable.StreamArn"}]


def test_the_stream_mapping_follows_adr_130(resources):
    """ADR #130 §6.1: INSERT only, from the start, retried until handled, no failure destination (D22)."""
    assert resources["AssetEventsTable"]["Properties"]["StreamSpecification"] == {"StreamViewType": "NEW_IMAGE"}
    mapping = resources["AssetEventsFanoutMapping"]["Properties"]
    assert mapping == {
        "EventSourceArn": {"!GetAtt": "AssetEventsTable.StreamArn"},
        "FunctionName": {"!Ref": "AssetFanoutFunction"},
        "StartingPosition": "TRIM_HORIZON",
        "BatchSize": 10,
        "MaximumRetryAttempts": -1,
        "MaximumRecordAgeInSeconds": -1,
        "BisectBatchOnFunctionError": True,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
        "FilterCriteria": {"Filters": [{"Pattern": '{"eventName": ["INSERT"]}'}]},
    }
    assert "AssetEventsFanoutMapping" in resources["RunTaskHelperSfn"]["DependsOn"]


def test_step_functions_actions_per_role(resources):
    def actions(role):
        return {a for s in _statements(resources, role) for a in s["Action"] if a.startswith("states:")}

    reconciler = {"states:SendTaskSuccess", "states:SendTaskFailure", "states:DescribeExecution"}
    assert actions("ReconcileRole") == reconciler
    assert actions("AssetFanoutRole") == reconciler | {"states:StartExecution"}
    assert actions("SweeperRole") == reconciler | {"states:StartExecution"}
    # The fake implements exactly the calls the code makes.
    assert {"states:" + "".join(p.title() for p in m.split("_")) for m in vars(FakeSfn) if not m.startswith("_")} \
        == reconciler | {"states:StartExecution"}


def test_describe_execution_is_scoped_to_wrapper_executions(resources):
    wrapper = next(r for r in resources.values()
                   if r["Type"] == "AWS::Serverless::StateMachine"
                   and r["Properties"]["Name"]["!Sub"].endswith("-polyris-dependency-wrapper"))
    statement = next(s for s in _statements(resources) if s["Action"] == ["states:DescribeExecution"])
    [arn] = statement["Resource"]
    assert arn["!Sub"].endswith(f":execution:{wrapper['Properties']['Name']['!Sub']}:*")
    assert "${AWS::Region}" in arn["!Sub"]      # not the AwsRegion parameter, which can drift


@pytest.mark.parametrize("function,module_name,role", [
    ("ReconcileFunction", "reconcile", "ReconcileRole"),
    ("AssetFanoutFunction", "asset_fanout", "AssetFanoutRole"),
    ("SweeperFunction", "sweeper", "SweeperRole"),
])
def test_function_wiring(resources, function, module_name, role):
    import importlib

    props = resources[function]["Properties"]
    module, handler = props["Handler"].split(".")
    assert (LAMBDA_ROOT.parent.parent / props["CodeUri"]).resolve() == LAMBDA_ROOT
    assert module == module_name and callable(getattr(importlib.import_module(module), handler))
    assert props["Role"] == {"!GetAtt": f"{role}.Arn"}
    variables = props["Environment"]["Variables"]
    expected = {var: TABLES[var] for var in FUNCTION_TABLES[function]}
    assert variables == {var: {"!Ref": logical} for var, (logical, _) in expected.items()}
    assert {var: getattr(env, var) for var in variables} == {var: name for var, (_, name) in expected.items()}


def test_time_budgets_fit_their_timeouts(resources):
    import asset_fanout

    # Resume's budget fits inside the timeout with room for the last row.
    assert reconcile.RESUME_BUDGET_SECONDS + 5 <= resources["ReconcileFunction"]["Properties"]["Timeout"]
    # AssetFanout stops starting records with RESERVE_MS left: one record's work fits in that reserve.
    assert asset_fanout.RESERVE_MS / 1000 < resources["AssetFanoutFunction"]["Properties"]["Timeout"]
    # The sweeper works within its budget, and its lease outlives a run that dies at the timeout
    # but not the next scheduled run (ADR #130 §8).
    from coordination_core import sweep

    timeout = resources["SweeperFunction"]["Properties"]["Timeout"]
    assert sweep.BUDGET.total_seconds() + 30 <= timeout < sweep.LEASE.total_seconds() < 5 * 60
