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

from boundary import as_component
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


def _clocked_datetime(clock: Callable[[], datetime]) -> type:
    """`datetime` whose `now()` reads the stack's clock: what a console module writes, such
    as a run's `resumed_at`, then agrees with the times the state machines and Lambdas write."""

    class StackDatetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:          # type: ignore[override]
            moment = clock()
            return moment.astimezone(tz) if tz is not None else moment.replace(tzinfo=None)

    return StackDatetime


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
        import routes.tasks as tasks

        tables = self.stack.tables
        self._set(config, "_sfn", self.stack.runtime)
        self._set(config, "_dynamodb", self.resource)
        self._set(config, "_dynamodb_client", self.stack.ddb)        # transactions and the specs' reads
        self._set(config, "_lambda", _LambdaClient(self.stack))
        for repo, logical in ((dal.executions_repo, "PipelineTokensTable"), (dal.backfills_repo, "PipelineTokensTable"),
                              (dal.pipelines_repo, "PipelineRegistryTable"), (dal.asset_events_repo, "AssetEventsTable"),
                              (dal.task_events_repo, "TaskEventsTable"), (dal.specs_repo, "PipelineSpecsTable")):
            self._set(repo, "_table_name", tables[logical])
        self._set(dal.specs_repo, "_versions", type(dal.specs_repo._versions)())   # no spec of another stack
        self._set(executions, "datetime", _clocked_datetime(self.clock))     # pause and resume times
        self._set(tasks, "datetime", _clocked_datetime(self.clock))          # an action's times and due time
        self._setenv("RESTART_HELPER_ARN", self.stack.machine_arn("RestartTaskHelperSfn"))
        return main

    def start_run(self) -> Callable[[Any, Any], Any]:
        """Start_Run over the stack's tables, on the stack's clock (ADR #137 §1)."""
        import env
        import start_run

        tables = self.stack.tables
        self._set(env, "_dynamodb", self.resource)
        self._set(env, "_dynamodb_client", self.stack.ddb)
        self._set(env, "TOKENS_TABLE", tables["PipelineTokensTable"])
        self._set(env, "SUBSCRIPTIONS_TABLE", tables["DependencySubscriptionsTable"])
        self._set(env, "SPECS_TABLE", tables["PipelineSpecsTable"])
        self._set(start_run, "datetime", _clocked_datetime(self.clock))
        handler = as_component("lambda:start-run", start_run.handler)
        self.stack.bind_lambda("StartRunFunction", handler)
        return handler

    def run_task(self, notifications: List[Dict[str, Any]]) -> str:
        """The stack's run_task state machine, as the template defines it (ADR #137 §1); returns its ARN.

        The notify function it calls is outside the engine: each call is recorded in `notifications` and
        answered as the free build answers an action it has no notifier for.
        """
        def notify(event: Dict[str, Any], _: Any) -> Dict[str, Any]:
            notifications.append(event)
            return {"action": event.get("action"), "delivered": False, "reason": "unknown_action"}

        self.stack.bind_lambda("NotifyFunction", as_component("lambda:notify", notify))
        return self.stack.machine_arn("RunTaskHelperSfn")

    def dispatcher(self, run_task_arn: str) -> Callable[[Any, Any], Any]:
        """The dispatcher over the stack's tables, on the stack's clock, under reconcile's function as the
        template binds it (ADR #137 §7); returns the handler the tasks stream's adapter feeds."""
        import dispatcher as module

        tables = self.stack.tables
        self._set(module, "_DISPATCHER", module.Dispatcher(
            self.stack.ddb, self.stack.runtime, resource=self.resource,
            tokens_table=tables["PipelineTokensTable"], subscriptions_table=tables["DependencySubscriptionsTable"],
            specs_table=tables["PipelineSpecsTable"], asset_events_table=tables["AssetEventsTable"],
            pipelines_table=tables["PipelineRegistryTable"], task_events_table=tables["TaskEventsTable"],
            run_task_arn=run_task_arn, clock=self.clock,
            sleep=lambda _: None))
        handler = as_component("lambda:reconcile", module.handler)
        self.stack.bind_lambda("ReconcileFunction", handler)
        return handler

    def asset_fanout(self) -> Any:
        """AssetFanout over the bound dispatcher; the stream adapter in world.py feeds it."""
        import asset_fanout

        import dispatcher

        fanout = asset_fanout.AssetFanout(dispatcher._DISPATCHER,
                                          subscriptions_table=self.stack.tables["DependencySubscriptionsTable"],
                                          consumers_table=self.stack.tables["AssetSubscriptionsTable"])
        self._set(asset_fanout, "_FANOUT", fanout)
        return asset_fanout.handler

    def sweeper(self) -> Callable[[Any, Any], Any]:
        """The sweeper over the bound dispatcher; World.sweep runs one scheduled invocation."""
        import sweeper

        import dispatcher

        self._set(sweeper, "_SWEEPER", sweeper.Sweeper(
            dispatcher._DISPATCHER, owner="harness",
            subscriptions_table=self.stack.tables["DependencySubscriptionsTable"],
            consumers_table=self.stack.tables["AssetSubscriptionsTable"],
            lambda_client=_LambdaClient(self.stack),
            fanout_function=self.stack.functions["AssetFanoutFunction"],
            dispatcher_function=self.stack.functions["ReconcileFunction"]))
        return sweeper.handler

    def query_subscriptions(self) -> Callable[[Any, Any], Any]:
        module = _load("query_subscriptions_handler", LAMBDAS / "query_subscriptions" / "index.py")
        import qs_dal

        self._set(qs_dal, "_dynamodb", self.resource)
        self._set(qs_dal.subscriptions_repo, "_explicit_table_name", self.stack.tables["DependencySubscriptionsTable"])
        handler = as_component("lambda:query-subscriptions", module.handler)
        self.stack.bind_lambda("QuerySubscriptionsFunction", handler)
        return handler
