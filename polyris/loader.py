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
from typing import Dict, Iterator, List, Union

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
    """Whether ``--all`` loads this .py file: ``dag.py``, or a file whose text mentions "polyris"
    and "dag".

    Exactly Airflow's safe mode, which loads a file only when it mentions "airflow" and "dag": a
    pipeline file names both, while a Glue or ECS script that uses ``xcom`` (and reads
    ``POLYRIS_PIPELINE_NAME``) names no DAG, and is never run. A pipeline in another file that
    never says "dag" (``Pipeline(...)``, a project wrapper) is skipped; name it ``dag.py`` or
    deploy it with ``--file``. Test files by pytest's convention (``test_*.py``, ``*_test.py``,
    ``conftest.py``) and ``__init__.py`` are not pipelines. ``polyris-validate --all`` and
    ``polyris-deploy --all`` / ``--only`` both use it.
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
    return "polyris" in text and "dag" in text


def child_env(**extra: str) -> Dict[str, str]:
    """The environment of a folder's own process in ``--all``: this one's, plus ``extra``.

    The child runs ``sys.executable -m polyris…`` and must import this very polyris. When
    polyris is installed (site-packages), the child finds it there as this process did. When it
    comes from elsewhere (a checkout run from source), its folder is appended to PYTHONPATH, after
    the user's own entries; never prepended, which would put site-packages before the standard
    library and a user's ``src/`` (sixth review of ADR #139).
    """
    import polyris

    env = {**os.environ, **extra}
    code_root = Path(polyris.__file__).resolve().parents[1]
    installed = {Path(p).resolve() for p in (*site.getsitepackages(), site.getusersitepackages(),
                                             sysconfig.get_paths()["purelib"], sysconfig.get_paths()["platlib"])}
    if code_root not in installed:
        env["PYTHONPATH"] = os.pathsep.join(p for p in (os.environ.get("PYTHONPATH", ""), str(code_root)) if p)
    return env


def exit_now(code: int) -> None:  # pragma: no cover -- ends the process (os._exit); runs only as a folder's own process exits
    """End a folder's own process once its results are written: flush, and do not wait for
    non-daemon threads a pipeline started, which would hold the next folder."""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


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
