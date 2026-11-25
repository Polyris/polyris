"""The coordination functions and roles in sam/template.yaml match the code (Principle #13).

The IAM tests record every DynamoDB call a function makes while it walks each of
its paths, then compare that set with its role: a call the role does not allow
would be an AccessDenied in production, and a grant no call uses is privilege
the function does not need. The dispatcher's walk also checks that every read a
decision rests on is consistent and uses no index (ADR #137 §7).
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
    STARTED_AT,
    TABLES,
    FUNCTION_ENV,
    FUNCTION_TABLES,
    FakeSfn,
    execution_name,
    legacy_fact,
    template,
)

import env

LOGICAL = {name: logical for logical, name in TABLES.values()}
Call = Tuple[str, str, Optional[str]]   # (IAM action, table logical id, index or None)
Read = Tuple[str, str, Optional[str], bool]   # (operation, table logical id, index or None, consistent)
_READS = {"GetItem", "BatchGetItem", "Query", "Scan"}


@pytest.fixture(scope="module")
def resources() -> Dict[str, Any]:
    return template()["Resources"]


@contextmanager
def recording(ddb, *clients: Any) -> Iterator[Set[Call]]:
    """Every DynamoDB call made through ``ddb`` and through the low-level ``clients``."""
    calls: Set[Call] = set()

    def before(params: Dict[str, Any], model: Any, **_: Any) -> None:
        if model.name == "TransactWriteItems":       # IAM authorizes each item's own action
            for item in params["TransactItems"]:
                [(kind, request)] = item.items()
                action = "ConditionCheckItem" if kind == "ConditionCheck" else f"{kind}Item"
                calls.add((f"dynamodb:{action}", LOGICAL[request["TableName"]], None))
            return
        tables = params["RequestItems"] if model.name == "BatchGetItem" else [params["TableName"]]
        for table in tables:
            calls.add((f"dynamodb:{model.name}", LOGICAL[table], params.get("IndexName")))

    every = [ddb.meta.client.meta.events, *(client.meta.events for client in clients)]
    for events in every:
        events.register("before-parameter-build.dynamodb", before, unique_id="coordination-iam")
    try:
        yield calls
    finally:
        for events in every:
            events.unregister("before-parameter-build.dynamodb", unique_id="coordination-iam")


@contextmanager
def reading(ddb, *clients: Any) -> Iterator[Set[Read]]:
    """Every DynamoDB read, with whether it is consistent (per table for BatchGetItem)."""
    reads: Set[Read] = set()

    def before(params: Dict[str, Any], model: Any, **_: Any) -> None:
        if model.name not in _READS:
            return
        if model.name == "BatchGetItem":
            for table, request in params["RequestItems"].items():
                reads.add((model.name, LOGICAL[table], None, bool(request.get("ConsistentRead"))))
            return
        reads.add((model.name, LOGICAL[params["TableName"]], params.get("IndexName"),
                   bool(params.get("ConsistentRead"))))

    every = [ddb.meta.client.meta.events, *(client.meta.events for client in clients)]
    for events in every:
        events.register("before-parameter-build.dynamodb", before, unique_id="coordination-reads")
    try:
        yield reads
    finally:
        for events in every:
            events.unregister("before-parameter-build.dynamodb", unique_id="coordination-reads")


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


def _walk_the_dispatcher(rows, ddb) -> Tuple[Set[Call], Set[Read]]:
    """The dispatcher's paths over a run Start_Run wrote: a start, a fact that wakes a row, the run's end."""
    from polyris.coordination import run_row

    run = "orders-run-2026-11-12-dddd0001"
    rows.start([{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"},
                {"task_id": "load", "task_type": "sfn", "dependencies": ["extract"], "trigger_rule": "all_success",
                 "wait_for": [{"asset_name": "ns/orders", "freshness_hours": None}]}], execution=run)
    client, dispatcher, sfn = rows.client, rows.dispatcher, rows.dispatcher.sfn
    tokens = TABLES["TOKENS_TABLE"][1]
    run_key = run_row.key(run)
    calls: Set[Call] = set()
    reads: Set[Read] = set()

    def walk(step: Any) -> None:
        with recording(ddb, client) as made, reading(ddb, client) as read:
            step()
        calls.update(made)
        reads.update(read)

    def pass_over(narrow: bool = False) -> Any:
        if narrow:
            return lambda: dispatcher.narrow_pass(run_key, [execution_name("load", run)], source="asset",
                                                  remaining_ms=lambda: 60_000)
        return lambda: dispatcher.full_pass(run_key, source="test", remaining_ms=lambda: 60_000)

    walk(pass_over())                                                            # extract starts
    client.update_item(TableName=tokens, Key={"execution_name": {"S": execution_name("extract", run)}},
                       UpdateExpression="SET #s = :s, finished_at = :f", ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":s": {"S": "success"}, ":f": {"S": "2026-11-12T05:59:00.000Z"}})
    ddb.Table(TABLES["ASSET_EVENTS_TABLE"][1]).put_item(Item=legacy_fact("ns/orders", "2026-11-12",
                                                                         event_time="2026-11-12T05:55:00.000Z"))
    walk(pass_over(narrow=True))                                                 # the fact starts load
    client.update_item(TableName=tokens, Key={"execution_name": {"S": execution_name("load", run)}},
                       UpdateExpression="SET #s = :s", ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":s": {"S": "success"}})
    walk(pass_over())                                                            # the run's end
    client.delete_item(TableName=tokens, Key={"execution_name": {"S": execution_name("load", run)}})
    walk(pass_over())                                                            # a missing row warns
    client.delete_item(TableName=tokens, Key={"execution_name": {"S": run_key}})
    walk(pass_over())                                                            # a missing run row stops its rows
    assert len(sfn.started) == 2 and sfn.succeeded, "the walk must start both rows and answer the run"

    # A second run: a Restart's pointers (those of the row it put back to wait), then a Stop of the run, which
    # stops a started row's run_task and the run's execution (ADR #137 §3, §4, §5).
    from datetime import timedelta

    from boto3.dynamodb.types import TypeDeserializer
    from polyris.coordination import actions
    from polyris.coordination.times import parse_utc
    _de = TypeDeserializer()
    other = "orders-run-2026-11-12-dddd0002"
    rows.start([{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"},
                {"task_id": "load", "task_type": "sfn", "dependencies": ["extract"], "trigger_rule": "all_success",
                 "wait_for": [{"asset_name": "ns/orders", "freshness_hours": None}]}], execution=other)
    other_key = run_row.key(other)

    def item(key: str) -> Dict[str, Any]:
        raw = client.get_item(TableName=tokens, Key={"execution_name": {"S": key}}, ConsistentRead=True)["Item"]
        return {k: _de.deserialize(v) for k, v in raw.items()}

    def full_pass_of_other() -> Any:
        return lambda: dispatcher.full_pass(other_key, source="test", remaining_ms=lambda: 60_000)

    walk(full_pass_of_other())                                                   # extract starts
    client.update_item(TableName=tokens, Key={"execution_name": {"S": execution_name("extract", other)}},
                       UpdateExpression="SET #s = :s, finished_at = :f", ExpressionAttributeNames={"#s": "status"},
                       ExpressionAttributeValues={":s": {"S": "success"}, ":f": {"S": "2026-11-12T05:59:00.000Z"}})
    now = parse_utc(str(item(other_key)[run_row.STARTED_AT])) + timedelta(hours=1)
    write = actions.restart(task=item(execution_name("extract", other)), run=item(other_key), with_dependants=False,
                            operator="ops", reason="rerun", now=now, tokens_table=tokens)
    client.transact_write_items(TransactItems=write.items)
    walk(full_pass_of_other())                                                   # the pointers; extract again
    client.update_item(**actions.stop_run(item(other_key), by="ops", reason="done", now=now, table=tokens))
    walk(full_pass_of_other())                                                   # the stop
    assert sfn.stopped, "the walk must stop a run_task and the run's execution"

    # A third run, whose execution is stopped in the AWS console: its end on EventBridge stops the run (§7).
    third = "orders-run-2026-11-12-dddd0003"
    rows.start([{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"}],
               execution=third)
    walk(lambda: dispatcher.full_pass(run_row.key(third), source="test", remaining_ms=lambda: 60_000))
    arn = str(item(run_row.key(third))[run_row.EXECUTION_ARN])
    sfn.executions[arn] = "ABORTED"
    walk(lambda: dispatcher.on_execution_end({"executionArn": arn, "name": third, "status": "ABORTED",
                                              "stateMachineArn": arn.rsplit(":", 1)[0]}, lambda: 60_000))
    assert item(run_row.key(third))["stop"]["by"] == run_row.ENGINE, "the walk must take an execution's end"

    # A fourth, whose task's run_task is stopped in the AWS console: its end fails the row at once (§7).
    from polyris.coordination import run_name, task_row

    fourth = "orders-run-2026-11-12-dddd0004"
    rows.start([{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"}],
               execution=fourth)
    walk(lambda: dispatcher.full_pass(run_row.key(fourth), source="test", remaining_ms=lambda: 60_000))
    row_key = task_row.key("extract", "2026-11-12", fourth)
    task_arn = f"{dispatcher.run_task_arn}:{run_name.run_task_name(row_key, STARTED_AT, 1)}"
    sfn.executions[task_arn] = "ABORTED"
    payload = json.dumps({"execution_name": row_key, "generation": 1, "pipeline_execution": fourth})
    walk(lambda: dispatcher.on_execution_end({"executionArn": task_arn, "name": task_arn.rsplit(":", 1)[-1],
                                              "status": "ABORTED", "stateMachineArn": dispatcher.run_task_arn,
                                              "input": payload}, lambda: 60_000))
    assert item(row_key)["status"] == "failed", "the walk must take a run_task's end"
    return calls, reads


def test_reconcile_role_allows_exactly_the_calls_the_code_makes(resources, ddb, rows):
    """The dispatcher's role: its managed policy, which AssetFanout and the sweeper attach too, and the stream."""
    used, _ = _walk_the_dispatcher(rows, ddb)
    granted = _dynamodb_grants(_statements(resources))
    assert used - granted == set(), "calls the role does not allow (AccessDenied in production)"
    assert granted - used == set(), "grants no call uses (drop them, or walk the path that needs them)"
    own = {p["PolicyName"]: p["PolicyDocument"]["Statement"] for p in resources["ReconcileRole"]["Properties"]["Policies"]}
    assert set(own) == {"DispatcherStreamPolicy", "DispatcherExecutionEndPolicy"}
    [stream] = own["DispatcherStreamPolicy"]
    assert set(stream["Action"]) == STREAM_ACTIONS and stream["Resource"] == [{"!GetAtt": "PipelineTokensTable.StreamArn"}]
    # The end of an execution: its pass judges the run's execution or a row's run_task (ADR #137 §1, §7). In the
    # role's own policy: AssetFanout attaches the managed one, and its narrow passes judge no execution.
    [describe] = own["DispatcherExecutionEndPolicy"]
    assert describe["Action"] == ["states:DescribeExecution"]


def test_every_read_of_the_dispatcher_is_consistent(ddb, rows):
    """A pass decides from consistent reads by key, with no index on the decision path (ADR #137 §7). A run whose
    run row is missing has its rows named by an index, and each is read again by key before its stop."""
    from polyris.coordination import task_row

    _, reads = _walk_the_dispatcher(rows, ddb)
    decisions = {r for r in reads if r[1] in {"PipelineTokensTable", "PipelineSpecsTable"}
                 and r[2] != task_row.RUN_ROWS_INDEX}
    assert decisions and {r for r in decisions if not r[3] or r[2] is not None} == set()


def test_the_tasks_stream_wakes_the_dispatcher_through_the_transition_tables_filters(resources):
    """ADR #137 §7: both images, from the start, retried until handled, no failure destination, and the
    filters the transition table generates."""
    from polyris.coordination import transitions

    tokens = resources["PipelineTokensTable"]["Properties"]
    assert tokens["StreamSpecification"] == {"StreamViewType": "NEW_AND_OLD_IMAGES"}
    mapping = {k: v for k, v in resources["TasksDispatchMapping"]["Properties"].items() if k != "Tags"}
    filters = mapping.pop("FilterCriteria")["Filters"]
    assert mapping == {
        "EventSourceArn": {"!GetAtt": "PipelineTokensTable.StreamArn"},
        "FunctionName": {"!Ref": "ReconcileFunction"},
        "StartingPosition": "TRIM_HORIZON",
        "BatchSize": 10,
        "MaximumBatchingWindowInSeconds": 1,
        "ParallelizationFactor": 2,
        "MaximumRetryAttempts": -1,
        "MaximumRecordAgeInSeconds": -1,
        "BisectBatchOnFunctionError": True,
        "FunctionResponseTypes": ["ReportBatchItemFailures"],
    }
    assert [json.loads(f["Pattern"]) for f in filters] == transitions.filter_patterns()
    assert len(filters) <= 4, "Lambda allows five filters per mapping by default; the ADR keeps one spare"


def _rule_pattern(resources: Dict[str, Any]) -> Dict[str, Any]:
    """The rule's EventPattern, its ``${...}`` left as they are."""
    return json.loads(resources["ExecutionEndRule"]["Properties"]["EventPattern"]["!Sub"])


def test_the_end_of_an_execution_wakes_the_dispatcher_through_its_rule(resources):
    """ADR #137 §7: an abnormal end of run_task or of a pipeline, not by the engine's own stop, on the default bus;
    the dispatcher, through a permission scoped to the rule. No queue, DLQ or retry policy (Principle #45)."""
    import dispatcher

    rule = resources["ExecutionEndRule"]["Properties"]
    assert rule["Name"] == {"!Sub": "${Namespace}-${Stage}-polyris-execution-end"}
    assert rule["State"] == "ENABLED" and "EventBusName" not in rule            # the default bus
    assert rule["Targets"] == [{"Id": "dispatcher", "Arn": {"!GetAtt": "ReconcileFunction.Arn"}}]
    assert _rule_pattern(resources) == {
        "source": [dispatcher.EVENT_SOURCE],
        "detail-type": ["Step Functions Execution Status Change"],
        "detail": {
            "status": ["ABORTED", "FAILED", "TIMED_OUT"],
            "error": [None, {"anything-but": dispatcher.STOPPED_ERROR}],
            "stateMachineArn": [{"wildcard": "arn:aws:states:${AWS::Region}:${AWS::AccountId}:stateMachine:*-polyris-*"}],
        },
    }
    assert resources["ExecutionEndPermission"]["Properties"] == {
        "FunctionName": {"!GetAtt": "ReconcileFunction.Arn"}, "Action": "lambda:InvokeFunction",
        "Principal": "events.amazonaws.com", "SourceArn": {"!GetAtt": "ExecutionEndRule.Arn"}}


def test_the_rule_names_the_run_task_the_dispatcher_tells_apart(resources):
    """The dispatcher takes an end whose stateMachineArn is its RUN_TASK_ARN for a run_task's (Principle #28)."""
    from coordination_testkit import RUN_TASK_ARN

    assert resources["ReconcileFunction"]["Properties"]["Environment"]["Variables"]["RUN_TASK_ARN"] == RUN_TASK_ARN
    # One wildcard takes the pipelines and the run_task machine alike: its name has "-polyris-" in it.
    [wildcard] = _rule_pattern(resources)["detail"]["stateMachineArn"]
    assert wildcard["wildcard"].endswith(":stateMachine:*-polyris-*") and "-polyris-" in RUN_TASK_ARN["!Sub"]


def test_the_dispatcher_starts_the_run_task_machine_by_its_name(resources):
    from coordination_testkit import RUN_TASK_ARN

    name = resources["RunTaskHelperSfn"]["Properties"]["Name"]["!Sub"]
    assert RUN_TASK_ARN["!Sub"].endswith(f":stateMachine:{name}") and "${AWS::Region}" in RUN_TASK_ARN["!Sub"]
    [start] = [s for s in _statements(resources) if s["Action"] == ["states:StartExecution"]]
    assert start["Resource"] == [RUN_TASK_ARN]


def test_asset_fanout_role_allows_every_call_and_its_own_grants_are_all_used(
        resources, rows, tokens, asset_events, consumers, sfn, ddb, mocker):
    import asset_fanout
    from coordination_testkit import client_error, fact

    fanout = asset_fanout.AssetFanout(rows.dispatcher, subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1])
    rows.start([{"task_id": "load", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success",
                 "wait_for": [{"asset_name": "ns/orders", "freshness_hours": None}]}])
    consumers.put_item(Item={"asset_name": "ns/orders", "pipeline_name": "gone", "sfn_arn": "arn:gone"})  # destroyed
    item = fact("ns/orders", "2026-11-12")
    record = {"eventName": "INSERT",
              "dynamodb": {"SequenceNumber": "1", "NewImage": {k: _serialize(v) for k, v in item.items()}}}
    asset_events.put_item(Item=item)
    with recording(ddb, rows.client) as used:
        assert fanout.handle([record], lambda: 10 ** 6) == {"batchItemFailures": []}
        mocker.patch.object(fanout.subscriptions, "query", side_effect=client_error("AccessDeniedException", "Query"))
        fanout.handle([record], lambda: 10 ** 6)           # a non-transient failure writes a warning
    assert sfn.started, "the walk must start the task its fact woke"
    assert "Item" not in consumers.get_item(Key={"asset_name": "ns/orders", "pipeline_name": "gone"})
    assert used - _dynamodb_grants(_statements(resources, "AssetFanoutRole")) == set()
    own = _dynamodb_grants(_statements(resources, "AssetFanoutRole", managed=False))
    assert own - used == set(), "AssetFanout's own grants that no call uses"


def test_sweeper_role_allows_every_call_and_its_own_grants_are_all_used(
        resources, rows, tokens, asset_events, consumers, registry, sfn, ddb):
    """The sweeper's paths: a due run, its liveness and execution checks and the engine's stop; a run whose run row
    is missing; a lost hint; a push retry, the catch-up's push and a stale subscription; the self-check."""
    from datetime import timedelta

    import sweeper
    from dispatcher import Dispatcher
    from polyris.coordination import attention as att
    from polyris.coordination import run_row
    from polyris.coordination.records import push_retry_item
    from polyris.coordination.times import format_utc
    from coordination_testkit import fact

    clock = NOW + timedelta(hours=1)
    dispatcher = Dispatcher(rows.client, sfn, resource=ddb, tokens_table=TABLES["TOKENS_TABLE"][1],
                            subscriptions_table=TABLES["SUBSCRIPTIONS_TABLE"][1], specs_table=TABLES["SPECS_TABLE"][1],
                            asset_events_table=TABLES["ASSET_EVENTS_TABLE"][1],
                            pipelines_table=TABLES["PIPELINES_TABLE"][1],
                            task_events_table=TABLES["TASK_EVENTS_TABLE"][1], run_task_arn="arn:run-task",
                            clock=lambda: clock, sleep=lambda _: None)

    class Mappings:
        def list_event_source_mappings(self, *, FunctionName: str) -> Dict[str, Any]:
            return {"EventSourceMappings": [{"State": "Enabled", "LastProcessingResult": "OK", "UUID": "u"}]}

    def sweep_once() -> None:
        sweeper.Sweeper(dispatcher, owner="me", lambda_client=Mappings(),
                        consumers_table=TABLES["ASSET_SUBSCRIPTIONS_TABLE"][1]).run()

    # A run whose activation's pass was lost, whose execution ended: its check starts extract, finds the lost hint,
    # then the engine stops the run, stops its rows, answers it and closes its run row.
    rows.start([{"task_id": "extract", "task_type": "sfn", "dependencies": [], "trigger_rule": "all_success"},
                {"task_id": "load", "task_type": "sfn", "dependencies": ["extract"], "trigger_rule": "all_success",
                 "wait_for": [{"asset_name": "ns/orders", "freshness_hours": None}]}])
    registry.put_item(Item={"pipeline_name": "orders", "sfn_arn": "arn:aws:states:us-east-1:1:stateMachine:orders"})
    sfn.executions[f"arn:aws:states:us-east-1:1:execution:orders:{RUN}"] = "ABORTED"
    # A row a 1.2 run left in flight: no run row.
    old = execution_name("old", "orders-run-2026-11-12-0ld0ld01")
    tokens.put_item(Item={"execution_name": old, "status": "waiting", "generation": 1,
                          "pipeline_execution": "orders-run-2026-11-12-0ld0ld01",
                          att.KEY: att.attention(att.WAITING, old), att.DUE: format_utc(clock - timedelta(minutes=5))})
    # A push retry, the catch-up's push, and a subscription its pipeline no longer has.
    consumers.put_item(Item={"asset_name": "ns/orders", "pipeline_name": "pushed", "sfn_arn": "arn:pushed"})
    consumers.put_item(Item={"asset_name": "ns/orders", "pipeline_name": "gone", "sfn_arn": "arn:gone"})
    registry.put_item(Item={"pipeline_name": "pushed",
                            "asset_schedule": json.dumps({"operator": "AND", "assets": ["ns/orders"]})})
    retry = push_retry_item(run_name="pushed-retry", consumer="pushed", sfn_arn="arn:pushed", run_input="{}",
                            error="ThrottlingException", now=clock - timedelta(hours=1))
    retry[att.DUE] = format_utc(clock - timedelta(minutes=1))
    tokens.put_item(Item=retry)
    asset_events.put_item(Item=fact("ns/orders", "2026-11-12", event_time=format_utc(clock - timedelta(hours=1))))
    with recording(ddb, rows.client) as used:
        sweep_once()
    stopped = tokens.get_item(Key={"execution_name": run_row.key(RUN)})["Item"]
    assert stopped["stop"]["by"] == "the engine" and sfn.failed, "the walk must stop the run its execution left"
    assert tokens.get_item(Key={"execution_name": old})["Item"]["status"] == "stopped"
    assert {s["name"] for s in sfn.started} >= {"pushed-retry"}
    assert "Item" not in consumers.get_item(Key={"asset_name": "ns/orders", "pipeline_name": "gone"})
    assert used - _dynamodb_grants(_statements(resources, "SweeperRole")) == set()
    own = _dynamodb_grants(_statements(resources, "SweeperRole", managed=False))
    assert own - used == set(), "the sweeper's own grants that no call uses"


def test_the_engines_grants_on_executions_cover_a_pipeline_of_any_namespace(resources):
    """The sweeper describes, and the dispatcher stops, run_task's executions and the pipelines' (ADR #137 §1, §4).
    A pipeline takes the namespace and stage of its own config.py: the examples' polyris-ex on a platform named
    polyris-dev-oss had every check of its runs denied (1.3 live smoke, D137)."""
    from fnmatch import fnmatch

    from polyris import DAG, task
    from polyris.deploy import _generate_cfn_template

    [describe] = [s for s in _statements(resources, "SweeperRole", managed=False)
                  if s["Action"] == ["states:DescribeExecution"]]
    [ended] = [s for s in _statements(resources, "ReconcileRole", managed=False)
               if s["Action"] == ["states:DescribeExecution"]]
    [stop] = [s for s in _statements(resources, "ReconcileRole") if s["Action"] == ["states:StopExecution"]]
    execution = "arn:aws:states:${AWS::Region}:${AWS::AccountId}:execution:"
    for statement in (describe, ended, stop):
        [arn] = statement["Resource"]
        assert arn["!Sub"] == execution + "*-polyris-*:*"
    with DAG("orders") as dag:
        @task.lambda_function(function_name="extract")
        def extract() -> None:
            return None
        extract()
    pipeline = _generate_cfn_template(dag, namespace="other", stage="qa", region="us-east-1",
                                      role_arn="arn:aws:iam::1:role/r", start_run_function_arn="arn:start-run",
                                      registry_table="r", tokens_table="t", asset_subscriptions_table="a")
    name = pipeline["Resources"]["PipelineStateMachine"]["Properties"]["StateMachineName"]
    run_task = resources["RunTaskHelperSfn"]["Properties"]["Name"]["!Sub"].replace("${Namespace}-${Stage}", "acme-dev")
    for machine in (name, run_task):
        assert fnmatch(f"{machine}:orders-run-2026-11-24-1a2b3c4d", "*-polyris-*:*"), machine


def test_start_run_role_allows_exactly_the_calls_the_code_makes_and_every_read_is_consistent(
        resources, ddb, monkeypatch):
    """Start_Run's paths: a first attempt, a retry that finds its rows, an earlier attempt that arrives late."""
    import boto3

    import start_run
    from polyris.coordination import spec_store, task_spec

    client = boto3.client("dynamodb", region_name="us-east-1")
    monkeypatch.setattr(env, "_dynamodb", ddb)
    monkeypatch.setattr(env, "_dynamodb_client", client)
    version = task_spec.build_version("orders", [{"task_id": "load", "task_type": "sfn", "dependencies": [],
                                                  "trigger_rule": "all_success",
                                                  "wait_for": [{"asset_name": "ns/orders"}]}])
    spec_store.write(client, TABLES["SPECS_TABLE"][1], version, sleep=lambda _: None)
    event = {"format_version": task_spec.FORMAT_VERSION, "spec_id": version.spec_id, "task_count": 1,
             "pipeline_name": "orders", "execution_name": RUN, "execution_arn": f"arn:aws:states:us-east-1:123456789012:execution:orders:{RUN}", "execution_start": STARTED_AT, "token": "t1",
             "retry_count": 0, "redrive_count": 0, "date": "2026-11-12", "input": {}}
    with recording(ddb, client) as used, reading(ddb, client) as reads:
        assert start_run.handler(event, None)["started"]
        assert start_run.handler({**event, "retry_count": 2, "token": "t3"}, None)["started"]
        assert not start_run.handler({**event, "retry_count": 1, "token": "t2"}, None)["started"]
    granted = _dynamodb_grants(_statements(resources, "StartRunRole"))
    assert used - granted == set(), "calls the role does not allow (AccessDenied in production)"
    assert granted - used == set(), "grants no call uses (drop them, or walk the path that needs them)"
    assert {read for read in reads if not read[3]} == set(), "a read that may miss a write just made"


def test_the_sweeper_runs_every_5_minutes_without_retries(resources):
    schedule = resources["SweeperSchedule"]["Properties"]
    assert schedule["ScheduleExpression"] == "rate(5 minutes)"
    assert schedule["Target"]["Arn"] == {"!GetAtt": "SweeperFunction.Arn"}
    assert schedule["Target"]["RetryPolicy"] == {"MaximumRetryAttempts": 0}
    [statement] = resources["SweeperScheduleRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
    assert (statement["Action"], statement["Resource"]) == (["lambda:InvokeFunction"], [{"!GetAtt": "SweeperFunction.Arn"}])


def test_the_sweeper_schedule_is_named_after_its_function(resources):
    """The console's finding runs `aws scheduler get-schedule --name <sweeper>` with the function name the
    sweeper reports: the same name, in the default group. The no-report finding names its suffix."""
    from polyris.coordination import sweep

    name = resources["SweeperFunction"]["Properties"]["FunctionName"]
    schedule = resources["SweeperSchedule"]["Properties"]
    assert schedule["Name"] == name and "GroupName" not in schedule
    [no_report] = sweep.engine_findings(None, NOW)
    assert name["!Sub"].endswith("-polyris-sweeper") and "-polyris-sweeper" in no_report["fix"]


@pytest.mark.parametrize("function,log_group", [("SweeperFunction", "SweeperLogGroup"),
                                                ("AssetFanoutFunction", "AssetFanoutLogGroup"),
                                                ("StartRunFunction", "StartRunLogGroup")])
def test_a_function_logs_to_the_group_the_console_commands_tail(resources, function, log_group):
    """`aws logs tail /aws/lambda/<name>` in the console's findings reads the function's own log group."""
    props = resources[function]["Properties"]
    assert props["LoggingConfig"]["LogGroup"] == {"!Ref": log_group}
    assert resources[log_group]["Properties"]["LogGroupName"] == {"!Sub": "/aws/lambda/" + props["FunctionName"]["!Sub"]}


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
    mapping = {k: v for k, v in resources["AssetEventsFanoutMapping"]["Properties"].items()
               if k != "Tags"}                     # billing tags: tests/sdk/test_resource_tags.py (ADR #132)
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

    dispatcher = {"states:SendTaskSuccess", "states:SendTaskFailure", "states:StartExecution", "states:StopExecution"}
    assert actions("ReconcileRole") == dispatcher | {"states:DescribeExecution"}     # an execution's end (§7)
    assert actions("AssetFanoutRole") == dispatcher
    assert actions("SweeperRole") == dispatcher | {"states:DescribeExecution"}
    # The fake implements exactly the calls the code makes.
    assert {"states:" + "".join(p.title() for p in m.split("_")) for m in vars(FakeSfn) if not m.startswith("_")} \
        == dispatcher | {"states:DescribeExecution"}


@pytest.mark.parametrize("function,module_name,role", [
    ("ReconcileFunction", "dispatcher", "ReconcileRole"),
    ("AssetFanoutFunction", "asset_fanout", "AssetFanoutRole"),
    ("SweeperFunction", "sweeper", "SweeperRole"),
    ("StartRunFunction", "start_run", "StartRunRole"),
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
    extra = FUNCTION_ENV.get(function, {})
    assert variables == {**{var: {"!Ref": logical} for var, (logical, _) in expected.items()}, **extra}
    assert {var: getattr(env, var) for var in expected} == {var: name for var, (_, name) in expected.items()}
    assert all(hasattr(env, var) for var in extra), "the code must read every variable the template sets"


def test_time_budgets_fit_their_timeouts(resources):
    import asset_fanout
    import dispatcher
    from polyris.coordination import start_run as start_run_core

    # Start_Run's retry window counts every attempt at the function's timeout (ADR #137 §1).
    assert resources["StartRunFunction"]["Properties"]["Timeout"] == start_run_core.TIMEOUT.total_seconds()

    # A pass keeps RESERVE_MS in hand below the dispatcher's timeout, and has time left to work (Rule 52).
    assert dispatcher.RESERVE_MS * 2 <= resources["ReconcileFunction"]["Properties"]["Timeout"] * 1000
    # AssetFanout stops starting records with RESERVE_MS left: one record's work fits in that reserve.
    assert asset_fanout.RESERVE_MS / 1000 < resources["AssetFanoutFunction"]["Properties"]["Timeout"]
    # The sweeper works within its budget, and its lease outlives a run that dies at the timeout
    # but not the next scheduled run (ADR #130 §8).
    from polyris.coordination import sweep

    timeout = resources["SweeperFunction"]["Properties"]["Timeout"]
    assert sweep.BUDGET.total_seconds() + 30 <= timeout < sweep.LEASE.total_seconds() < 5 * 60
