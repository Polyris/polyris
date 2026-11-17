"""One way to run a pipeline file and find the DAGs it defines (ADR #139).

``polyris-validate``, ``polyris-deploy``, ``polyris-output`` and ``polyris-local`` all load a
pipeline file through ``load_pipeline``, so a file they disagree about cannot exist: until
1.2.2 each had its own loader, and three times a file passed ``polyris-validate`` and then
could not be deployed (backlog F18 and the reviews of its fix).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterator, List, Union

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
    """Whether ``--all`` loads this .py file: ``dag.py``, or a file whose text mentions polyris and
    "dag" or "pipeline".

    Airflow's safe mode, which loads a file only when it mentions "airflow" and "dag": a pipeline
    file names both, while a Glue or Lambda script that uses ``from polyris import xcom`` does
    not, and is never run. A pipeline in another file that never mentions polyris (it imports a
    wrapper) is listed as skipped; name it ``dag.py`` or deploy it with ``--file``. Test files by
    pytest's convention (``test_*.py``, ``*_test.py``, ``conftest.py``) and ``__init__.py`` are not
    pipelines. ``polyris-validate --all`` and ``polyris-deploy --all`` / ``--only`` both use it.
    """
    name = path.name
    if name in _NOT_PIPELINES or name.startswith("test_") or name.endswith("_test.py"):
        return False
    if name == "dag.py":
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:  # unreadable (permissions): not loaded, and listed as skipped by the caller
        return False
    return "polyris" in text and ("dag" in text or "pipeline" in text)


@contextmanager
def open_pipeline(path: Union[str, Path]) -> Iterator[LoadedPipeline]:
    """Run the pipeline file at ``path``; inside the block, its imports stay as the file left them.

    The file's folder and the folder above it are importable while it runs and while the block
    runs, so a pipeline can import a sibling module or its project's ``config``, and a task body
    that ``polyris-local`` or ``polyris-validate --test`` runs can too; on leaving the block they
    are removed. Modules the file imports stay imported: ``--all`` runs each folder in its own
    process, so nothing one folder imports reaches the next (ADR #139). ``sys.exit(0)`` (or a bare ``sys.exit()``) ends
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


def load_pipeline(path: Union[str, Path]) -> LoadedPipeline:
    """``open_pipeline`` for a caller that only needs the DAGs: cleaned up on return."""
    with open_pipeline(path) as loaded:
        return loaded
