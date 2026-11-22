"""Builders and fakes shared by the coordination Lambda tests.

A plain module, not conftest: in a combined `pytest sam/lambdas` run another
directory's conftest can own the module name `conftest`.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml
from botocore.exceptions import ClientError

LAMBDA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LAMBDA_ROOT.parents[2]

TABLES = {
    "TOKENS_TABLE": ("PipelineTokensTable", "test-pipeline-tokens"),
    "SUBSCRIPTIONS_TABLE": ("DependencySubscriptionsTable", "test-dependency-subscriptions"),
    "ASSET_EVENTS_TABLE": ("AssetEventsTable", "test-asset-events"),
    "PIPELINES_TABLE": ("PipelineRegistryTable", "test-pipeline-registry"),
    "ASSET_SUBSCRIPTIONS_TABLE": ("AssetSubscriptionsTable", "test-asset-subscriptions"),
    "SPECS_TABLE": ("PipelineSpecsTable", "test-pipeline-specs"),
    "TASK_EVENTS_TABLE": ("TaskEventsTable", "test-task-events"),
}
#: Variables each coordination function reads (the others' tables it never touches).
FUNCTION_TABLES = {
    "ReconcileFunction": ("TOKENS_TABLE", "SUBSCRIPTIONS_TABLE", "ASSET_EVENTS_TABLE", "PIPELINES_TABLE",
                          "SPECS_TABLE", "TASK_EVENTS_TABLE"),
    "AssetFanoutFunction": ("TOKENS_TABLE", "SUBSCRIPTIONS_TABLE", "ASSET_EVENTS_TABLE", "PIPELINES_TABLE",
                            "ASSET_SUBSCRIPTIONS_TABLE", "SPECS_TABLE", "TASK_EVENTS_TABLE"),
    "SweeperFunction": ("TOKENS_TABLE", "SUBSCRIPTIONS_TABLE", "ASSET_EVENTS_TABLE", "PIPELINES_TABLE",
                        "ASSET_SUBSCRIPTIONS_TABLE", "SPECS_TABLE", "TASK_EVENTS_TABLE"),
    "StartRunFunction": ("TOKENS_TABLE", "SUBSCRIPTIONS_TABLE", "SPECS_TABLE"),
}
#: Environment a function reads beyond its tables.
#: The run_task state machine by its name: a !Ref would close a dependency cycle (ADR #137 §1).
RUN_TASK_ARN = {"!Sub": "arn:aws:states:${AWS::Region}:${AWS::AccountId}:stateMachine:${Namespace}-${Stage}-polyris-dep-run-task-helper"}
FUNCTION_ENV = {
    "SweeperFunction": {"ASSET_FANOUT_FUNCTION": {"!Ref": "AssetFanoutFunction"}, "RUN_TASK_ARN": RUN_TASK_ARN,
                        "DISPATCHER_FUNCTION": {"!Ref": "ReconcileFunction"}},
    "ReconcileFunction": {"RUN_TASK_ARN": RUN_TASK_ARN},
    "AssetFanoutFunction": {"RUN_TASK_ARN": RUN_TASK_ARN},
}

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
RUN = "orders-run-2026-11-12-abcdef12"
DATE = "2026-11-12"
STARTED_AT = "2026-11-12T05:00:00.000Z"


def template() -> Dict[str, Any]:
    """sam/template.yaml; an intrinsic function becomes ``{"!GetAtt": "X.Arn"}`` and so on."""
    class Loader(yaml.SafeLoader):
        pass

    def intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            return {f"!{suffix}": loader.construct_scalar(node)}
        if isinstance(node, yaml.SequenceNode):
            return {f"!{suffix}": loader.construct_sequence(node, deep=True)}
        return {f"!{suffix}": loader.construct_mapping(node, deep=True)}

    Loader.add_multi_constructor("!", intrinsic)
    return yaml.load((REPO_ROOT / "sam" / "template.yaml").read_text(), Loader=Loader)


def create_tables(resource: Any, tables: Dict[str, str]) -> None:
    """Create ``{logical id: table name}`` in moto exactly as sam/template.yaml declares them.

    Key schemas, indexes, projections and streams are the deployed ones
    (Principle #13). Shared with the console tests, which read the same tables.
    """
    resources = template()["Resources"]
    for logical, name in tables.items():
        props = resources[logical]["Properties"]
        kwargs: Dict[str, Any] = {
            "TableName": name,
            "KeySchema": props["KeySchema"],
            "AttributeDefinitions": props["AttributeDefinitions"],
            "BillingMode": "PAY_PER_REQUEST",
        }
        if props.get("GlobalSecondaryIndexes"):
            kwargs["GlobalSecondaryIndexes"] = [
                {k: gsi[k] for k in ("IndexName", "KeySchema", "Projection")}
                for gsi in props["GlobalSecondaryIndexes"]
            ]
        if props.get("StreamSpecification"):
            kwargs["StreamSpecification"] = {"StreamEnabled": True, **props["StreamSpecification"]}
        resource.create_table(**kwargs)


def client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} (test)"}}, operation)


class FakeSfn:
    """The Step Functions calls the coordination code makes."""

    def __init__(self) -> None:
        self.succeeded: List[Dict[str, Any]] = []
        self.started: List[Dict[str, Any]] = []
        self.failed: List[Dict[str, Any]] = []
        self.dead_tokens: set = set()
        self.executions: Dict[str, Any] = {}
        self.described: List[str] = []
        self.stopped: List[Dict[str, Any]] = []
        self.errors: Dict[str, Exception] = {}

    def _raise_if_set(self, operation: str) -> None:
        if operation in self.errors:
            raise self.errors[operation]

    def send_task_success(self, *, taskToken: str, output: str) -> Dict[str, Any]:
        self._raise_if_set("send_task_success")
        if taskToken in self.dead_tokens:
            raise client_error("TaskTimedOut", "SendTaskSuccess")
        self.succeeded.append({"token": taskToken, "output": json.loads(output)})
        self.dead_tokens.add(taskToken)   # a token is consumed once
        return {}

    def send_task_failure(self, *, taskToken: str, error: str, cause: str) -> Dict[str, Any]:
        self._raise_if_set("send_task_failure")
        if taskToken in self.dead_tokens:
            raise client_error("TaskTimedOut", "SendTaskFailure")
        self.failed.append({"token": taskToken, "error": error, "cause": cause})
        self.dead_tokens.add(taskToken)
        return {}

    def start_execution(self, *, stateMachineArn: str, name: str, input: str) -> Dict[str, Any]:
        self._raise_if_set("start_execution")
        if name in {s["name"] for s in self.started}:
            raise client_error("ExecutionAlreadyExists", "StartExecution")
        self.started.append({"arn": stateMachineArn, "name": name, "input": json.loads(input)})
        return {"executionArn": f"{stateMachineArn}:{name}"}

    def stop_execution(self, *, executionArn: str, error: str, cause: str) -> Dict[str, Any]:
        self._raise_if_set("stop_execution")
        self.stopped.append({"arn": executionArn, "error": error, "cause": cause})
        return {"stopDate": "2026-11-12T06:00:00Z"}

    def describe_execution(self, *, executionArn: str) -> Dict[str, Any]:
        """``executions`` maps an ARN to its status, or to the status with its ``error`` and ``cause``."""
        self._raise_if_set("describe_execution")
        self.described.append(executionArn)
        if executionArn not in self.executions:
            raise client_error("ExecutionDoesNotExist", "DescribeExecution")
        known = self.executions[executionArn]
        return {"executionArn": executionArn, **(known if isinstance(known, dict) else {"status": known})}


def execution_name(task: str, run: str = RUN, date: str = DATE) -> str:
    return f"{task}-{date}-{run[-20:]}"


def fact(asset: str, key: str, *, event_time: str = "2026-11-12T04:00:00.000Z", **extra: Any) -> Dict[str, Any]:
    """An asset-events row as an ADR #130 writer stores it."""
    from polyris.coordination.partition import partition_date
    from polyris.partitions import detect_granularity

    item: Dict[str, Any] = {
        "asset_name": asset,
        "event_time": event_time,
        "execution_date": key,
        "partition_key": key,
        "partition_date": partition_date(key).isoformat(),
        "granularity": detect_granularity(key),
        "cascade": "auto",
        "source_dag": "producer",
        "source_task": "build",
        "uri": f"s3://bucket/{asset}",
        "fact_version": 2,
    }
    item.update(extra)
    return item


def legacy_fact(asset: str, key: str, *, event_time: str) -> Dict[str, Any]:
    """An asset-events row written before ADR #130: no partition fields, no granularity."""
    return {"asset_name": asset, "event_time": event_time, "execution_date": key,
            "source_dag": "producer", "source_task": "build"}
