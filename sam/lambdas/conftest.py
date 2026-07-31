"""
Lambda-suite test isolation.

Each Lambda package ships its own ``index.py`` / ``dal/`` / ``logger.py`` at
the same relative path. A combined ``pytest sam/lambdas/...`` run collects test
files from multiple packages in one interpreter session, so the first
``from index import ...`` cached in ``sys.modules['index']`` shadows every
subsequent Lambda's own ``index`` module — tests that appeared green in
isolation fail with ``ImportError`` or land on the wrong module's ``handler``
under the combined run.

The Makefile target ``make test-lambdas`` sidesteps this by running each
Lambda's tests in a separate subprocess (``cd sam/lambdas/<X> && pytest ...``)
with a per-Lambda ``PYTHONPATH=.``. This conftest brings the same isolation to
manual ``pytest sam/lambdas/`` invocations so a fresh contributor doesn't
chase a flaky-looking failure that's really an import cache collision.

Mechanism (pytest hook, per test setup — not per collection):
  1. Take the Lambda root directory (parent of the test file within
     ``sam/lambdas/``) and place it first on ``sys.path``.
  2. Drop the cached ``index``, ``dal``, ``logger`` (and their sub-modules)
     from ``sys.modules`` so the next ``import`` resolves against the current
     Lambda's files, not the previous test's.

``pytest_runtest_setup`` is the right hook, not ``pytest_collectstart``,
because collection and execution are separate phases: cleaning at collection
gets overwritten by later collectors before tests actually run.

If a test file lives outside a Lambda subdirectory (e.g. tests/), the hook is
a no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Modules that every Lambda ships under the same name — clearing these forces
# the next test's imports to resolve fresh.
_SHARED_LAMBDA_MODULES = ("index", "dal", "logger", "constants", "constants_generated")

_LAMBDAS_ROOT = Path(__file__).parent.resolve()


def _lambda_root_for(test_path: Path) -> Path | None:
    """Return the Lambda's own directory (e.g. sam/lambdas/evaluate_deps) that
    a test file belongs to, or None if the test isn't nested under one, or if
    the Lambda uses a full package layout (like ``console_api`` with its own
    ``main.py`` + ``dal/`` subpackage). Full-package Lambdas don't hit the
    ``index.py`` collision this hook was written for, and clearing their
    ``dal`` cache mid-run breaks other tests that import from
    ``dal.task_events_repo`` etc."""
    try:
        rel = test_path.resolve().relative_to(_LAMBDAS_ROOT)
    except ValueError:
        return None
    if not rel.parts:
        return None
    lambda_root = _LAMBDAS_ROOT / rel.parts[0]
    # Only lambdas that ship a single-file ``index.py`` handler collide on the
    # shared ``index`` module name. Skip anything else.
    if not (lambda_root / "index.py").is_file():
        return None
    return lambda_root


def _reset_for(lambda_root: Path) -> None:
    """Force sys.path + sys.modules to reflect the given Lambda's own tree."""
    lambda_root_str = str(lambda_root)
    if lambda_root_str in sys.path:
        sys.path.remove(lambda_root_str)
    sys.path.insert(0, lambda_root_str)
    # Drop shared-name modules and their sub-packages so the next ``import``
    # binds to the current Lambda's files.
    for cached in [k for k in list(sys.modules) if any(
        k == m or k.startswith(m + ".") for m in _SHARED_LAMBDA_MODULES
    )]:
        sys.modules.pop(cached, None)


def pytest_runtest_setup(item):
    """Fires before each test. Reset the Lambda module cache so tests from a
    different Lambda that ran earlier can't leak their ``index``/``dal``
    modules into this test's imports."""
    test_path = Path(str(item.fspath))
    lambda_root = _lambda_root_for(test_path)
    if lambda_root is None:
        return
    _reset_for(lambda_root)
