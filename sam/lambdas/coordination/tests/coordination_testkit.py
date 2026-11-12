"""Builders and fakes shared by the coordination Lambda tests.

A plain module, not conftest: in a combined `pytest sam/lambdas` run another
directory's conftest can own the module name `conftest`.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from botocore.exceptions import ClientError

LAMBDA_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = LAMBDA_ROOT.parents[2]

TABLES = {
    "TOKENS_TABLE": ("PipelineTokensTable", "test-pipeline-tokens"),
    "SUBSCRIPTIONS_TABLE": ("DependencySubscriptionsTable", "test-dependency-subscriptions"),
    "ASSET_EVENTS_TABLE": ("AssetEventsTable", "test-asset-events"),
    "PIPELINES_TABLE": ("PipelineRegistryTable", "test-pipeline-registry"),
}

NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
RUN = "orders-run-2026-11-12-abcdef12"
SHORT = RUN[-20:]   # "-2026-11-12-abcdef12", as the generated pipeline SFN derives it
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


def client_error(code: str, operation: str = "Operation") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} (test)"}}, operation)


class FakeSfn:
    """The Step Functions calls the coordination code makes."""

    def __init__(self) -> None:
        self.succeeded: List[Dict[str, Any]] = []
        self.failed: List[Dict[str, Any]] = []
        self.dead_tokens: set = set()
        self.executions: Dict[str, str] = {}
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

    def describe_execution(self, *, executionArn: str) -> Dict[str, Any]:
        self._raise_if_set("describe_execution")
        if executionArn not in self.executions:
            raise client_error("ExecutionDoesNotExist", "DescribeExecution")
        return {"executionArn": executionArn, "status": self.executions[executionArn]}


def execution_name(task: str, run: str = RUN, date: str = DATE) -> str:
    return f"{task}-{date}-{run[-20:]}"


def task_row(task: str, *, deps: Optional[List[str]] = None, wait_for: Optional[List[Any]] = None,
             status: str = "waiting", trigger_rule: str = "all_success", **extra: Any) -> Dict[str, Any]:
    """A task row as the registration SFN's Save_Task_Record writes it."""
    name = execution_name(task)
    row: Dict[str, Any] = {
        "execution_name": name,
        "task_name": task,
        "status": status,
        "dependencies": json.dumps(deps or []),
        "wait_for": json.dumps(wait_for or []),
        "trigger_rule": trigger_rule,
        "date": DATE,
        "started_at": STARTED_AT,
        "pipeline_name": "orders",
        "pipeline_execution": RUN,
        "pipeline_execution_short": SHORT,
        "wait_token": f"token-{task}",
        "orchestration_token": f"orchestration-{task}",
        "wrapper_execution_arn": f"arn:aws:states:us-east-1:123456789012:execution:wrapper:{name}",
    }
    row.update(extra)
    return row


def fact(asset: str, key: str, *, event_time: str = "2026-11-12T04:00:00.000Z", **extra: Any) -> Dict[str, Any]:
    """An asset-events row as an ADR #130 writer stores it."""
    from coordination_core.partition import partition_date
    from coordination_core.partitions import detect_granularity

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
