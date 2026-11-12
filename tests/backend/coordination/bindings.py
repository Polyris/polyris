"""Platform Lambdas served in-process by their real handlers, wired to the harness's tables.

Each binding points a function's module-level clients and table names at the
stack, and `close()` puts them back, so no test leaks state into the next.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from types import ModuleType
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from platform_stack import Stack

LAMBDAS = Path(__file__).resolve().parents[3] / "sam" / "lambdas"


def _load(name: str, path: Path) -> ModuleType:
    """Import a Lambda's handler module under a unique name (every Lambda calls its own `index`)."""
    directory = str(path.parent)
    if directory not in sys.path:
        sys.path.append(directory)   # appended: the coordination modules keep precedence
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _LambdaClient:
    """A boto3 Lambda client whose RequestResponse invokes run the stack's in-process handlers."""

    def __init__(self, stack: Stack):
        self.stack = stack

    def list_event_source_mappings(self, *, FunctionName: str) -> Dict[str, Any]:
        """The stack's mappings are always enabled: StreamAdapter delivers their records."""
        return {"EventSourceMappings": [{"FunctionName": FunctionName, "State": "Enabled"}]}

    def invoke(self, *, FunctionName: str, Payload: Any = b"{}", InvocationType: str = "RequestResponse") -> Dict[str, Any]:
        handler = self.stack.runtime.lambdas[FunctionName.rsplit(":", 1)[-1]]
        try:
            result = handler(json.loads(Payload or "{}"), None)
        except Exception as exc:  # noqa: BLE001 — as Lambda reports an unhandled error to its caller
            body = {"errorType": type(exc).__name__, "errorMessage": str(exc)}
            return {"StatusCode": 200, "FunctionError": "Unhandled", "Payload": io.BytesIO(json.dumps(body).encode())}
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result, default=str).encode())}


class Bindings:
    def __init__(self, stack: Stack, resource: Any, clock: Callable[[], datetime]):
        self.stack = stack
        self.resource = resource
        self.clock = clock
        self._undo: List[Tuple[Any, str, Any]] = []
        self._env: Dict[str, Optional[str]] = {}

    def _set(self, target: Any, attribute: str, value: Any) -> None:
        self._undo.append((target, attribute, getattr(target, attribute)))
        setattr(target, attribute, value)

    def _setenv(self, name: str, value: str) -> None:
        self._env.setdefault(name, os.environ.get(name))
        os.environ[name] = value

    def close(self) -> None:
        for target, attribute, value in reversed(self._undo):
            setattr(target, attribute, value)
        self._undo.clear()
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._env.clear()

    def console(self) -> Any:
        """The console API's real handler over the stack; returns its `main` module.

        Its clients are lazy proxies over three module globals in `config`, so
        those point at the interpreter (Step Functions), moto (DynamoDB) and the
        in-process handlers (Lambda); each repository gets its table's name.
        """
        directory = str(LAMBDAS / "console_api")
        if directory not in sys.path:
            sys.path.append(directory)   # appended: the coordination modules keep precedence
        import config
        import dal
        import main
        import routes.executions as executions
        import task_actions

        tables = self.stack.tables
        self._set(config, "_sfn", self.stack.runtime)
        self._set(config, "_dynamodb", self.resource)
        self._set(config, "_lambda", _LambdaClient(self.stack))
        for repo, logical in ((dal.executions_repo, "PipelineTokensTable"), (dal.backfills_repo, "PipelineTokensTable"),
                              (dal.pipelines_repo, "PipelineRegistryTable"), (dal.asset_events_repo, "AssetEventsTable"),
                              (dal.dep_subscriptions_repo, "DependencySubscriptionsTable"),
                              (dal.task_events_repo, "TaskEventsTable")):
            self._set(repo, "_table_name", tables[logical])
        self._set(executions, "RECONCILE_FUNCTION_ARN", self.stack.function_arn("ReconcileFunction"))
        self._set(task_actions, "NOTIFY_DEPENDENTS_HELPER_ARN", self.stack.machine_arn("NotifyDependentsSfn"))
        self._setenv("RESTART_HELPER_ARN", self.stack.machine_arn("RestartTaskHelperSfn"))
        return main

    def reconcile(self) -> Callable[[Any, Any], Any]:
        import reconcile

        tables = self.stack.tables
        self._set(reconcile, "_RECONCILER", reconcile.Reconciler(
            self.resource, self.stack.runtime,
            tokens_table=tables["PipelineTokensTable"],
            subscriptions_table=tables["DependencySubscriptionsTable"],
            asset_events_table=tables["AssetEventsTable"],
            pipelines_table=tables["PipelineRegistryTable"],
            clock=self.clock,
        ))
        self.stack.bind_lambda("ReconcileFunction", reconcile.handler)
        return reconcile.handler

    def asset_fanout(self) -> Any:
        """AssetFanout over the bound reconciler; the stream adapter in world.py feeds it."""
        import asset_fanout
        import reconcile

        fanout = asset_fanout.AssetFanout(reconcile._RECONCILER,
                                          subscriptions_table=self.stack.tables["DependencySubscriptionsTable"],
                                          consumers_table=self.stack.tables["AssetSubscriptionsTable"])
        self._set(asset_fanout, "_FANOUT", fanout)
        return asset_fanout.handler

    def sweeper(self) -> Callable[[Any, Any], Any]:
        """The sweeper over the bound reconciler; World.sweep runs one scheduled invocation."""
        import reconcile
        import sweeper

        self._set(sweeper, "_SWEEPER", sweeper.Sweeper(
            reconcile._RECONCILER, owner="harness",
            subscriptions_table=self.stack.tables["DependencySubscriptionsTable"],
            consumers_table=self.stack.tables["AssetSubscriptionsTable"],
            lambda_client=_LambdaClient(self.stack),
            fanout_function=self.stack.functions["AssetFanoutFunction"]))
        return sweeper.handler

    def query_subscriptions(self) -> Callable[[Any, Any], Any]:
        module = _load("query_subscriptions_handler", LAMBDAS / "query_subscriptions" / "index.py")
        import qs_dal

        self._set(qs_dal, "_dynamodb", self.resource)
        self._set(qs_dal.subscriptions_repo, "_explicit_table_name", self.stack.tables["DependencySubscriptionsTable"])
        self.stack.bind_lambda("QuerySubscriptionsFunction", module.handler)
        return module.handler
