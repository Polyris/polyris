"""Platform Lambdas served in-process by their real handlers, wired to the harness's tables.

Each binding points a function's module-level clients and table names at the
stack, and `close()` puts them back, so no test leaks state into the next.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from datetime import datetime
from typing import Any, Callable, List, Tuple

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


class Bindings:
    def __init__(self, stack: Stack, resource: Any, clock: Callable[[], datetime]):
        self.stack = stack
        self.resource = resource
        self.clock = clock
        self._undo: List[Tuple[Any, str, Any]] = []

    def _set(self, target: Any, attribute: str, value: Any) -> None:
        self._undo.append((target, attribute, getattr(target, attribute)))
        setattr(target, attribute, value)

    def close(self) -> None:
        for target, attribute, value in reversed(self._undo):
            setattr(target, attribute, value)
        self._undo.clear()

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

    def query_subscriptions(self) -> Callable[[Any, Any], Any]:
        module = _load("query_subscriptions_handler", LAMBDAS / "query_subscriptions" / "index.py")
        import qs_dal

        self._set(qs_dal, "_dynamodb", self.resource)
        self._set(qs_dal.subscriptions_repo, "_explicit_table_name", self.stack.tables["DependencySubscriptionsTable"])
        self.stack.bind_lambda("QuerySubscriptionsFunction", module.handler)
        return module.handler
