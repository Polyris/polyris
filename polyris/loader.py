"""One way to run a pipeline file and find the DAGs it defines (ADR #139).

``polyris-validate``, ``polyris-deploy``, ``polyris-output`` and ``polyris-local`` all load a
pipeline file through ``load_pipeline``, so a file they disagree about cannot exist: until
1.2.2 each had its own loader, and three times a file passed ``polyris-validate`` and then
could not be deployed (backlog F18 and the reviews of its fix).
"""

from __future__ import annotations

import importlib.util
import os
import site
import sys
import sysconfig
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterable, Iterator, List, Optional, Set, Union

from .dag import DAG, recording_dags

#: The ``__name__`` every pipeline file runs under, whichever command loads it.
MODULE_NAME = "polyris_pipeline"

#: pytest's conftest and package markers: never loaded as pipelines.
_NOT_PIPELINES = ("conftest.py", "__init__.py")


class PipelineLoadError(Exception):
    """A pipeline file could not be run, or defines one dag_id twice; the message says why."""


class PipelineNotFoundError(PipelineLoadError):
    """The pipeline file does not exist."""


@dataclass
class LoadedPipeline:
    path: Path
    module: ModuleType
    #: Every DAG the file's code made, in the order it made them (``recording_dags``).
    dags: List[DAG]


def is_pipeline_file(path: Path) -> bool:
    """Whether ``--all`` loads this .py file: ``dag.py``, or a file whose text mentions polyris.

    The rule Airflow's safe mode uses (a DAG file mentions "airflow"): a pipeline always
    imports polyris, so none is skipped, and a Glue script or a migration next to it is never
    run. Test files by pytest's convention (``test_*.py``, ``*_test.py``, ``conftest.py``) and
    ``__init__.py`` are not pipelines. ``polyris-validate --all`` and ``polyris-deploy --all`` /
    ``--only`` both use it.
    """
    name = path.name
    if name in _NOT_PIPELINES or name.startswith("test_") or name.endswith("_test.py"):
        return False
    if name == "dag.py":
        return True
    try:
        return "polyris" in path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:  # unreadable (permissions): not loaded, and listed as skipped by the caller
        return False


def _installed_roots() -> List[Path]:
    """Where installed code lives: the standard library and every site-packages."""
    roots: Set[str] = set()
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        where = sysconfig.get_paths().get(key)
        if where:
            roots.add(where)
    roots.update(site.getsitepackages())
    user_site = site.getusersitepackages()
    if isinstance(user_site, str):
        roots.add(user_site)
    return [Path(r).resolve() for r in roots]


def _origin(module: ModuleType) -> Optional[Path]:
    """Where a module's code lives, read without running a module ``__getattr__``."""
    attrs = vars(module)
    spec = attrs.get("__spec__")
    where = attrs.get("__file__") or getattr(spec, "origin", None)
    if not where or where in ("built-in", "frozen"):
        locations = getattr(spec, "submodule_search_locations", None) or attrs.get("__path__") or []
        where = next(iter(locations), None)  # a namespace package: its folder
    return Path(where).resolve() if where else None


def _forget_imports(before: Set[str], installed: Iterable[Path]) -> None:
    """Forget every module the file imported first, so the next file imports its own.

    The file's ``settings.py`` — in its folder, in a ``src/`` it put on the path, behind a
    symlink — would otherwise be handed to the next file in ``--all``. Installed code (the
    standard library, site-packages) and ``polyris`` stay: they are the same for every file,
    and a second copy of such a module would be a second copy of its classes.
    """
    roots = list(installed)
    for name in [n for n in list(sys.modules) if n not in before]:
        if name == "polyris" or name.startswith("polyris."):
            continue
        module = sys.modules.get(name)
        where = _origin(module) if isinstance(module, ModuleType) else None
        if where is None or any(where.is_relative_to(root) for root in roots):
            continue
        del sys.modules[name]


@contextmanager
def open_pipeline(path: Union[str, Path]) -> Iterator[LoadedPipeline]:
    """Run the pipeline file at ``path``; inside the block, its imports stay as the file left them.

    The file's folder and the folder above it are importable while it runs and while the block
    runs, so a pipeline can import a sibling module or its project's ``config``, and a task body
    that ``polyris-local`` or ``polyris-validate --test`` runs can too. On leaving the block they
    are removed and every module the file imported first is forgotten (``_forget_imports``), so
    the next file in ``--all`` imports its own. ``sys.exit(0)`` (or a bare ``sys.exit()``) ends
    the file normally. Raises ``PipelineNotFoundError`` for a missing file and
    ``PipelineLoadError`` for an error, a non-zero exit, or one dag_id made twice: each DAG is
    one stack, so the second would replace the first. A file without DAGs loads with an empty
    list; the caller says what that means for it.
    """
    given = path
    path = Path(os.path.abspath(path))  # absolute and without "..": "dag.py" has a folder above it
    if not path.is_file():
        raise PipelineNotFoundError(f"Pipeline file not found: {given}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:  # pragma: no cover -- defensive: returns a loaded spec for an existing .py file
        raise PipelineLoadError(f"Cannot load module from {path}")
    module = importlib.util.module_from_spec(spec)

    added = [p for p in (str(path.parent), str(path.parent.parent)) if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    before = set(sys.modules)
    try:
        with recording_dags(sys._getframe()) as made:
            try:
                spec.loader.exec_module(module)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise PipelineLoadError(f"sys.exit({e.code})") from None
            except Exception as e:
                raise PipelineLoadError(str(e) or type(e).__name__) from e
        ids = [d.dag_id for d in made]
        twice = sorted({i for i in ids if ids.count(i) > 1})
        if twice:
            raise PipelineLoadError(
                f"{path.name} defines {', '.join(repr(i) for i in twice)} more than once: "
                "each DAG is one stack, so the second would replace the first"
            )
        yield LoadedPipeline(path=path, module=module, dags=list(made))
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)
        _forget_imports(before, _installed_roots())


def load_pipeline(path: Union[str, Path]) -> LoadedPipeline:
    """``open_pipeline`` for a caller that only needs the DAGs: cleaned up on return."""
    with open_pipeline(path) as loaded:
        return loaded
