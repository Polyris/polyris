"""Plugin route registration for the console API (ADR #97).

A small **explicit** registry that lets each route module register its own routes
instead of listing every route centrally in ``main.py``. ``main.py`` becomes the
runner: it holds an explicit list of route modules and calls each module's
``register(router)``.

This is the seam the open-core split rides on. Open-core builds register the
open-core route modules; a proprietary build additionally registers the modules
shipped in the proprietary package. What is registered determines the API
surface. Registration is **explicit** (a listed set of modules), not implicit
package discovery — explicit is easier to read, order, and reason about.

Behaviour is identical to the previous flat ``ROUTES`` dict: a route is a
``(METHOD, path) -> (handler, param_key)`` mapping, dispatched the same way.

``param_key`` semantics (unchanged):
    None        -> handler(event)
    "name"/"id" -> handler(param_value, event)   (with required-param check)
"""
from typing import Callable, Dict, Optional, Protocol, Tuple

# (handler, param_key)
Route = Tuple[Callable, Optional[str]]
RouteKey = Tuple[str, str]  # (METHOD, path)


class Router:
    """Collects ``(METHOD, path) -> (handler, param_key)`` route entries."""

    def __init__(self) -> None:
        self._table: Dict[RouteKey, Route] = {}

    def add(self, method: str, path: str, handler: Callable, param: Optional[str] = None) -> None:
        """Register one route. Raises on a duplicate (METHOD, path)."""
        key = (method, path)
        if key in self._table:
            raise ValueError(f"duplicate route: {method} {path}")
        self._table[key] = (handler, param)

    def get(self, method: str, path: str) -> Optional[Route]:
        return self._table.get((method, path))

    @property
    def table(self) -> Dict[RouteKey, Route]:
        """The full ``(METHOD, path) -> (handler, param_key)`` mapping."""
        return self._table


class RouteModule(Protocol):
    """Structural type for a route plugin: a module exposing ``register(router)``.

    ``main.py`` holds an explicit list of these and calls ``register`` on each.
    """

    def register(self, router: Router) -> None: ...
